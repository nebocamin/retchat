"""Switching Reticulum interfaces at runtime (ReticulumService)."""

import threading
import types

import pytest

import retchat.reticulum_service as rs
from retchat.reticulum_service import ReticulumService

CONFIG = """[reticulum]
  share_instance = Yes
[interfaces]
  [[Default Interface]]
    type = AutoInterface
    enabled = no
  [[Hub]]
    type = TCPClientInterface
    enabled = yes
    target_host = example.org
    target_port = 4242
"""


class FakeReticulum:
    """Stands in for RNS.Reticulum.get_instance()."""

    def __init__(self):
        self.running = {"Hub"}
        self.calls = []
        self.block = None  # threading.Event: the call hangs until set
        self.result = {}   # action -> forced result
        self.raises = None

    def _do(self, action, name):
        self.calls.append((action, name))
        if self.block is not None:
            self.block.wait()
        if self.raises:
            raise self.raises
        if action in self.result:
            return self.result[action]
        if action == "attach":
            if name in self.running:
                return False
            self.running.add(name)
            return True
        if action == "detach":
            if name not in self.running:
                return None
            self.running.discard(name)
            return True
        if name not in self.running:
            return None
        return True

    def attach_interface(self, name): return self._do("attach", name)
    def detach_interface(self, name): return self._do("detach", name)
    def reload_interface(self, name): return self._do("reload", name)

    def get_interface_stats(self):
        return {"interfaces": [{"name": f"TCPInterface[{n}]", "short_name": n, "type": "TCPClientInterface",
                                "status": True, "rxb": 10, "txb": 20} for n in sorted(self.running)]}


@pytest.fixture
def rns(monkeypatch):
    fake = FakeReticulum()
    monkeypatch.setattr(rs.RNS.Reticulum, "get_instance", staticmethod(lambda: fake))
    return fake


@pytest.fixture
def service(tmp_path, monkeypatch):
    (tmp_path / "config").write_text(CONFIG)
    svc = ReticulumService.__new__(ReticulumService)
    svc._rnsconfigdir = str(tmp_path)
    return svc


@pytest.fixture
def results(monkeypatch):
    """Collect on_applied calls; GLib.idle_add runs the callback directly."""
    got = []
    done = threading.Event()
    monkeypatch.setattr(rs.GLib, "idle_add", lambda fn, *a: fn(*a))

    def on_applied(name, outcome):
        got.append((name, outcome))
        done.set()

    def wait():
        assert done.wait(5), "on_applied not called"
        done.clear()
        return got[-1]

    return types.SimpleNamespace(cb=on_applied, wait=wait)


def _config(service):
    from RNS.vendor.configobj import ConfigObj
    return ConfigObj(service._config_path())["interfaces"]


def test_switch_on_attaches(service, rns, results):
    assert service.set_interface_enabled("Default Interface", True, results.cb)
    assert results.wait() == ("Default Interface", "applied")
    assert rns.calls == [("attach", "Default Interface")]
    assert _config(service)["Default Interface"]["enabled"] == "yes"


def test_switch_off_detaches(service, rns, results):
    assert service.set_interface_enabled("Hub", False, results.cb)
    assert results.wait() == ("Hub", "applied")
    assert rns.calls == [("detach", "Hub")]
    cfg = _config(service)["Hub"]
    assert (cfg["enabled"], cfg["interface_enabled"]) == ("no", "false")


def test_switch_off_not_running_is_fine(service, rns, results):
    service.set_interface_enabled("Default Interface", False, results.cb)
    assert results.wait() == ("Default Interface", "applied")


def test_attach_already_running(service, rns, results):
    service.set_interface_enabled("Hub", True, results.cb)
    assert results.wait() == ("Hub", "applied")


def test_unknown_interface(service, rns):
    assert service.set_interface_enabled("Nope", True) is False
    assert rns.calls == []


def test_attach_without_config_entry_on_instance(service, rns, results):
    rns.result["attach"] = None  # e.g. a shared instance with another config
    service.set_interface_enabled("Default Interface", True, results.cb)
    assert results.wait() == ("Default Interface", "error")


def test_management_disabled(service, rns, results):
    rns.result["detach"] = False
    service.set_interface_enabled("Hub", False, results.cb)
    assert results.wait() == ("Hub", "restart")


def test_rpc_error_means_restart(service, rns, results):
    rns.raises = ConnectionRefusedError()
    service.set_interface_enabled("Hub", False, results.cb)
    assert results.wait() == ("Hub", "restart")


def test_unanswered_rpc_times_out(service, rns, results, monkeypatch):
    """An older shared instance never answers the request."""
    monkeypatch.setattr(rs, "INTERFACE_APPLY_TIMEOUT", 0.2)
    rns.block = threading.Event()
    try:
        service.set_interface_enabled("Hub", False, results.cb)
        assert results.wait() == ("Hub", "restart")
        # The change is saved for the next start
        assert _config(service)["Hub"]["enabled"] == "no"
    finally:
        rns.block.set()


def test_tcp_save_reloads_running_interface(service, rns, results):
    service.save_tcp_settings("hub.example", "7777", on_applied=results.cb)
    assert results.wait() == ("Hub", "applied")
    assert rns.calls == [("reload", "Hub")]
    cfg = _config(service)
    assert (cfg["Hub"]["target_host"], cfg["Hub"]["target_port"]) == ("hub.example", "7777")
    # AutoInterface untouched (used to be switched on by every save)
    assert cfg["Default Interface"]["enabled"] == "no"


def test_tcp_save_starts_stopped_interface(service, rns, results):
    rns.running.clear()
    service.save_tcp_settings("hub.example", "7777", on_applied=results.cb)
    assert results.wait() == ("Hub", "applied")
    assert rns.calls == [("reload", "Hub"), ("attach", "Hub")]


def test_configured_interfaces_use_instance_status(service, rns):
    by_name = {i["name"]: i for i in service.get_configured_interfaces()}
    assert by_name["Hub"]["online"] and by_name["Hub"]["rxb"] == 10
    assert not by_name["Default Interface"]["online"]
    assert not by_name["Default Interface"]["enabled"]
    assert service.get_interfaces_info() == [
        {"name": "Hub", "type": "TCPClientInterface", "online": True, "rxb": 10, "txb": 20}]


def test_config_path_follows_rns(tmp_path, monkeypatch):
    svc = ReticulumService.__new__(ReticulumService)
    svc._rnsconfigdir = None
    monkeypatch.setattr(rs.RNS.Reticulum, "configpath", "/x/config")
    assert svc._config_path() == "/x/config"
    monkeypatch.setattr(rs.RNS.Reticulum, "configpath", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    if not __import__("os").path.isfile("/etc/reticulum/config"):
        assert svc._config_path() == str(tmp_path / ".reticulum" / "config")
        (tmp_path / ".config" / "reticulum").mkdir(parents=True)
        (tmp_path / ".config" / "reticulum" / "config").write_text("")
        assert svc._config_path() == str(tmp_path / ".config" / "reticulum" / "config")
