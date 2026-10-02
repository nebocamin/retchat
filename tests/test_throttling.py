"""Batching of announces, chat list refreshes and LXMF stamp cost saves."""

import threading
import types

import pytest

import retchat.reticulum_service as rs
from retchat.database import Database
from retchat.reticulum_service import ReticulumService

PEER_A = "aa" * 16
PEER_B = "bb" * 16


class _FakeRouter:
    def __init__(self):
        self.outbound_stamp_costs = {}
        self.saves = 0

    def update_stamp_cost(self, destination_hash, stamp_cost):
        raise AssertionError("LXMF's own update_stamp_cost must be replaced")

    def save_outbound_stamp_costs(self):
        self.saves += 1


@pytest.fixture
def timers(monkeypatch):
    """Capture GLib.timeout_add instead of running a main loop."""
    pending = []
    monkeypatch.setattr(rs.GLib, "timeout_add", lambda ms, fn, *a: pending.append((ms, fn, a)) or len(pending))
    return pending


@pytest.fixture
def service(tmp_path):
    svc = ReticulumService.__new__(ReticulumService)
    svc.db = Database(str(tmp_path / "test.db"))
    svc.app = types.SimpleNamespace(message_router=_FakeRouter())
    svc._node_names = {}
    svc._announce_lock = threading.Lock()
    svc._announce_batch = {}
    svc._announce_flush_scheduled = False
    svc._announce_callbacks = []
    svc._conv_changed_lock = threading.Lock()
    svc._conv_changed_scheduled = False
    svc._conversations_changed_callbacks = []
    svc._stamp_cost_router = None
    svc._stamp_costs_dirty = False
    svc._stamp_costs_saved_at = 0.0
    return svc


def test_stamp_costs_saved_at_most_once_per_interval(service, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: now[0])
    service._throttle_stamp_cost_saves()
    router = service.app.message_router

    for i in range(50):
        router.update_stamp_cost(bytes([i]) * 16, 8)
    assert len(router.outbound_stamp_costs) == 50
    assert router.saves == 0  # nothing written per announce

    service._save_stamp_costs()
    assert router.saves == 1
    router.update_stamp_cost(b"\x01" * 16, 9)
    service._save_stamp_costs()
    assert router.saves == 1  # within the interval

    now[0] += rs.STAMP_COST_SAVE_INTERVAL
    service._save_stamp_costs()
    assert router.saves == 2
    service._save_stamp_costs(force=True)
    assert router.saves == 2  # nothing changed since


def test_stamp_costs_saved_at_exit(service):
    service._throttle_stamp_cost_saves()
    service.app.message_router.update_stamp_cost(b"\x01" * 16, 8)
    service._save_stamp_costs(force=True)
    assert service.app.message_router.saves == 1


def test_announces_batched_newest_per_destination(service, timers, monkeypatch):
    monkeypatch.setattr(rs.RNS.Transport, "hops_to", lambda h: 2)
    clock = iter(range(100, 200))
    monkeypatch.setattr(rs.time, "time", lambda: float(next(clock)))
    batches = []
    service.add_announce_callback(batches.append)

    service._dispatch_directory_announce(bytes.fromhex(PEER_A), b"Alice", "peer")
    service._dispatch_directory_announce(bytes.fromhex(PEER_B), b"Node", "node")
    service._dispatch_directory_announce(bytes.fromhex(PEER_A), b"Alice 2", "peer")

    assert len(timers) == 1  # one flush for all of them
    assert timers[0][0] == int(rs.ANNOUNCE_BATCH_INTERVAL * 1000)
    assert batches == []

    _ms, flush, args = timers[0]
    assert flush(*args) is False
    assert len(batches) == 1
    batch = batches[0]
    assert [d["destination_hash"] for d in batch] == [PEER_B, PEER_A]  # oldest first
    assert batch[1]["display_name"] == "Alice 2"
    assert service._node_names[PEER_B] == "Node"

    service._dispatch_directory_announce(bytes.fromhex(PEER_A), b"Alice", "peer")
    assert len(timers) == 2  # next batch scheduled again


def test_conversations_changed_merged(service, timers):
    calls = []
    service.add_conversations_changed_callback(lambda: calls.append(1))

    for _ in range(10):
        service._on_conversations_changed_nomadnet()
    assert len(timers) == 1
    assert calls == []

    _ms, flush, args = timers[0]
    flush(*args)
    assert calls == [1]
    service._schedule_conversations_changed()
    assert len(timers) == 2


def test_database_shared_connection_across_threads(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    errors = []

    def worker(n):
        try:
            for i in range(50):
                dest = f"{n:02x}{i:02x}" * 8
                db.set_custom_name(dest, f"name {n} {i}")
                assert db.get_custom_name(dest) == f"name {n} {i}"
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(db.get_all_custom_names()) == 200
