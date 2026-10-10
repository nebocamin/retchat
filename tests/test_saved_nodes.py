"""Saved NomadNet nodes (NomadNet's directory) and recognising what an address is."""

import types

import pytest
import RNS

import retchat.reticulum_service as rs
from nomadnet import Directory
from nomadnet.Directory import DirectoryEntry
from retchat.reticulum_service import ReticulumService

NODE_ID = RNS.Identity()
NODE = RNS.Destination.hash(NODE_ID, "nomadnetwork", "node").hex()
OPERATOR = RNS.Destination.hash(NODE_ID, "lxmf", "delivery").hex()
PROPAGATION = RNS.Destination.hash(NODE_ID, "lxmf", "propagation").hex()


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(Directory, "load_from_disk", lambda self: None)
    monkeypatch.setattr(RNS.Transport, "register_announce_handler", lambda handler: None)
    monkeypatch.setattr(RNS.Transport, "hops_to", lambda h: 2)
    app = types.SimpleNamespace(compact_stream=True, ui=None, directorypath=str(tmp_path / "directory"),
                                lxmf_destination=types.SimpleNamespace(hash=bytes(16)))
    app.directory = Directory(app)
    svc = ReticulumService.__new__(ReticulumService)
    svc.app = app
    svc._node_names = {NODE: "Mesh Café"}
    return svc


def test_save_rename_forget(service, tmp_path):
    assert service.saved_nodes() == []
    service.save_node(NODE)
    assert service.is_saved_node(NODE)
    assert service.saved_nodes() == [{"destination_hash": NODE, "name": "Mesh Café", "hops": 2}]
    assert (tmp_path / "directory").exists()  # NomadNet's file, shared with the terminal client
    entry = service.app.directory.find(bytes.fromhex(NODE))
    assert entry.hosts_node and entry.trust_level == DirectoryEntry.UNKNOWN  # not "trusted"

    service.rename_node(NODE, "Café")
    assert service.saved_nodes()[0]["name"] == "Café"
    assert service.forget_node(NODE) == "Café"
    assert not service.is_saved_node(NODE) and service.app.directory.find(bytes.fromhex(NODE)) is None
    service.save_node(NODE, "Café")  # undo
    assert service.saved_nodes()[0]["name"] == "Café"


def test_forget_keeps_trusted_entry(service):
    entry = DirectoryEntry(bytes.fromhex(NODE), "Mesh Café", DirectoryEntry.TRUSTED, hosts_node=True)
    service.app.directory.remember(entry)
    service.forget_node(NODE)
    kept = service.app.directory.find(bytes.fromhex(NODE))
    assert kept is not None and kept.trust_level == DirectoryEntry.TRUSTED and not kept.hosts_node
    service.save_node(NODE)
    assert kept.hosts_node and kept.trust_level == DirectoryEntry.TRUSTED


@pytest.fixture
def known(monkeypatch):
    """RNS knows NODE_ID (as if its announce was heard)."""
    def recall(h, from_identity_hash=False, _no_use=False):
        if from_identity_hash:
            return NODE_ID if h == NODE_ID.hash else None
        return NODE_ID if h.hex() in (NODE, OPERATOR, PROPAGATION) else None
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(recall))
    monkeypatch.setattr(RNS.Identity, "recall_app_data", staticmethod(lambda h: None))


def test_address_kind(service, known):
    assert service.address_kind(NODE) == ("node", "Mesh Café")
    assert service.address_kind(OPERATOR) == ("contact", None)
    assert service.address_kind(PROPAGATION) == ("propagation", None)
    assert service.address_kind(NODE_ID.hash.hex()) == ("identity", None)
    assert service.address_kind("ab" * 16) == ("unknown", None)
    assert service.addresses_for_identity(NODE_ID.hash.hex()) == {"contact": OPERATOR, "node": NODE}
