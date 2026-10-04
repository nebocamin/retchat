"""Internals of RNS, LXMF and NomadNet that Retchat relies on.

Retchat embeds NomadNet and hooks into it (replaced methods, callbacks,
tuple layouts). Most hooks check that their target exists and otherwise
do nothing, so a library update that renames something would degrade
silently (e.g. the stamp cost file being rewritten per announce again).
These tests fail instead. Run them after changing a pin in
requirements.txt; see "Updating dependencies" in README.md.
"""

import inspect
import os
import sys
import threading
import types
from importlib.metadata import version

import pytest

import LXMF
import RNS
import RNS.vendor.umsgpack as umsgpack
import nomadnet
import nomadnet.Conversation as conversation_module
from LXMF.Handlers import LXMFDeliveryAnnounceHandler
from LXMF.LXMRouter import LXMRouter
from nomadnet import Conversation, Directory, NomadNetworkApp
from nomadnet.Conversation import ConversationMessage

# The packages export classes under their modules' names
transport_module = sys.modules["RNS.Transport"]
directory_module = sys.modules["nomadnet.Directory"]

REQUIREMENTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt")
PEER = bytes.fromhex("aa" * 16)
NODE = bytes.fromhex("bb" * 16)


def _pins():
    pins = {}
    with open(REQUIREMENTS) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                name, ver = line.split("==")
                pins[name.lower()] = ver
    return pins


def _params(func):
    return list(inspect.signature(func).parameters)


def _source(obj):
    return inspect.getsource(obj)


# --- Versions -------------------------------------------------------------------

@pytest.mark.parametrize("package", ["rns", "lxmf", "nomadnet"])
def test_reticulum_stack_matches_pins(package):
    """The tests run against the versions that get packaged.

    If this fails, update the venv: pip install --no-deps -r requirements.txt
    """
    assert version(package) == _pins()[package]


# --- LXMF: stamp costs (ReticulumService._throttle_stamp_cost_saves) -------------------

def test_stamp_cost_methods():
    assert _params(LXMRouter.update_stamp_cost) == ["self", "destination_hash", "stamp_cost"]
    assert _params(LXMRouter.save_outbound_stamp_costs) == ["self"]
    init = _source(LXMRouter.__init__)
    assert "self.outbound_stamp_costs" in init
    # Entries are [timestamp, cost]; the replacement writes the same layout.
    assert "[time.time(), stamp_cost]" in _source(LXMRouter.update_stamp_cost)


def test_delivery_announces_go_through_update_stamp_cost():
    """The replaced method is what LXMF calls per announce."""
    calls = []
    router = types.SimpleNamespace(update_stamp_cost=lambda h, c: calls.append((h, c)), pending_outbound=[])
    handler = LXMFDeliveryAnnounceHandler(router)
    handler.received_announce(PEER, None, umsgpack.packb([b"Alice", 8]))
    assert calls == [(PEER, 8)]


def test_save_outbound_stamp_costs_needs_only_known_attributes(tmp_path):
    router = types.SimpleNamespace(storagepath=str(tmp_path), cost_file_lock=threading.Lock(),
                                   outbound_stamp_costs={PEER: [1.0, 8]})
    LXMRouter.save_outbound_stamp_costs(router)
    with open(tmp_path / "outbound_stamp_costs", "rb") as f:
        assert umsgpack.unpackb(f.read()) == {PEER: [1.0, 8]}


# --- NomadNet app (_RetchatApp, ReticulumService) -----------------------------------------

def test_app_constructor_and_overridden_methods():
    params = _params(NomadNetworkApp.__init__)
    assert params[:4] == ["self", "configdir", "rnsconfigdir", "daemon"]
    assert _params(NomadNetworkApp.lxmf_delivery) == ["self", "message"]
    assert _params(NomadNetworkApp.get_shared_instance) == []


@pytest.mark.parametrize("name", [
    "announce_now", "autoselect_propagation_node", "exit_handler", "get_default_propagation_node",
    "get_display_name", "get_sync_progress", "get_sync_status", "get_user_selected_propagation_node",
    "mark_conversation_read", "request_lxmf_sync", "set_display_name", "set_user_selected_propagation_node",
])
def test_app_methods(name):
    assert callable(getattr(NomadNetworkApp, name))


@pytest.mark.parametrize("attribute", [
    "attachmentpath", "cachepath", "conversationpath", "directory", "identity",
    "lxmf_destination", "message_router", "node", "compact_stream",
])
def test_app_attributes(attribute):
    assert f"self.{attribute}" in _source(NomadNetworkApp.__init__)


def test_quiet_ui_hook():
    """install_quiet_ui replaces nomadnet.ui.spawn, which the app calls."""
    assert _params(nomadnet.ui.spawn) == ["uimode"]
    assert "nomadnet.ui.spawn(" in _source(NomadNetworkApp.__init__)


# --- Directory (announce hooks, announce stream) ----------------------------------------------

@pytest.fixture
def directory(monkeypatch):
    monkeypatch.setattr(Directory, "load_from_disk", lambda self: None)
    monkeypatch.setattr(RNS.Transport, "register_announce_handler", lambda handler: None)
    return Directory(types.SimpleNamespace(compact_stream=True, ui=None))


def test_directory_hook_signatures():
    assert _params(Directory.lxmf_announce_received) == ["self", "source_hash", "app_data"]
    assert _params(Directory.node_announce_received) == ["self", "source_hash", "app_data", "associated_peer"]
    assert _params(Directory.display_name) == ["self", "source_hash"]


def test_announce_stream_layout(directory):
    """get_announces reads (timestamp, source_hash, app_data, kind)."""
    directory.lxmf_announce_received(PEER, b"Alice")
    directory.node_announce_received(NODE, b"Node", None)
    entries = {e[1]: e for e in directory.announce_stream}
    timestamp, source_hash, app_data, kind = entries[PEER][:4]
    assert isinstance(timestamp, float) and source_hash == PEER and app_data == b"Alice" and kind == "peer"
    assert entries[NODE][2:4] == (b"Node", "node")
    assert directory.display_name(bytes(16)) is None


def test_directory_hooks_are_called_from_announce_handlers():
    """Retchat replaces the instance methods; NomadNet must call them via the instance."""
    assert "app.directory.lxmf_announce_received(" in _source(conversation_module)
    assert "app.directory.node_announce_received(" in _source(directory_module)


# --- Conversation ----------------------------------------------------------------------

def test_conversation_class_attributes():
    assert Conversation.created_callback is None
    assert isinstance(Conversation.unread_conversations, dict)
    assert isinstance(Conversation.cached_conversations, dict)
    source = _source(conversation_module)
    # Called for stored messages and announces of peers with a chat
    assert source.count("Conversation.created_callback()") >= 2


def test_conversation_signatures():
    assert _params(Conversation.__init__)[:3] == ["self", "source_hash", "app"]
    assert _params(Conversation.conversation_list) == ["app"]
    assert _params(Conversation.query_for_peer) == ["source_hash"]
    assert _params(Conversation.delete_conversation) == ["source_hash_path", "app"]
    assert "fields" in _params(Conversation.send)
    assert callable(Conversation.ensure_send_destination)
    assert callable(Conversation.scan_storage)


def test_message_notification_hook():
    """install_delivery_hook wraps the class attribute; send() must register
    it per message, looked up through the instance."""
    assert _params(Conversation.message_notification) == ["self", "message"]
    assert "self.message_notification" in _source(Conversation.send)


def test_conversation_list_layout(tmp_path, monkeypatch):
    """get_conversations unpacks (hash, display name, trust, sort name, unread, activity, failed)."""
    conv_dir = tmp_path / PEER.hex()
    conv_dir.mkdir()
    (conv_dir / "unread").write_text("3")
    monkeypatch.setattr(Conversation, "unread_conversations", {})
    monkeypatch.setattr(Conversation, "failed_conversations", {}, raising=False)
    monkeypatch.setattr(RNS.Identity, "recall_app_data", lambda h: None)
    app = types.SimpleNamespace(
        conversationpath=str(tmp_path),
        directory=types.SimpleNamespace(display_name=lambda h: "Alice", trust_level=lambda h, dn=None: 2),
    )
    (entry,) = Conversation.conversation_list(app)
    source_hash, display_name, trust, sort_name, unread, activity, failed = entry[:7]
    assert source_hash == PEER.hex()
    assert display_name == "Alice"
    assert unread == 3
    assert activity == os.path.getmtime(conv_dir)
    # get_conversation reads the unread count the same way
    assert Conversation.unread_conversations[PEER] == 3


@pytest.mark.parametrize("name", [
    "get_content", "get_fields", "get_hash", "get_state", "get_timestamp", "get_title",
    "has_attachments", "load", "unload",
])
def test_conversation_message_methods(name):
    assert callable(getattr(ConversationMessage, name))


def test_conversation_message_attributes():
    source = _source(ConversationMessage)
    for attribute in ("self.loaded", "self.lxm", "self.sort_timestamp", "self._cached_source_hash"):
        assert attribute in source
    assert _params(ConversationMessage.extract_attachments_from_lxm) == ["lxmessage", "app"]


# --- RNS and LXMF ---------------------------------------------------------------------

def test_rns_api():
    for name in ("hops_to", "has_path", "request_path"):
        assert callable(getattr(RNS.Transport, name))
    assert isinstance(RNS.Transport.path_table, dict)
    assert isinstance(RNS.Transport.PATHFINDER_M, int)
    # request_path's worker reads path_table[dest][0] as the entry's timestamp
    assert transport_module.IDX_PT_TIMESTAMP == 0
    for name in ("recall", "recall_app_data", "full_hash", "truncated_hash"):
        assert callable(getattr(RNS.Identity, name))
    assert callable(RNS.Destination.hash_from_name_and_identity)
    assert isinstance(RNS.Reticulum.TRUNCATED_HASHLENGTH, int)
    assert RNS.Link.ACTIVE != RNS.Link.CLOSED


def test_interface_management_api():
    """_apply_interface_change and the interfaces dialog (RNS >= 1.5.6)."""
    for action in ("attach", "detach", "reload"):
        method = getattr(RNS.Reticulum, f"{action}_interface")
        assert _params(method) == ["self", "interface_name"]
        source = _source(method)
        # Forwarded to a shared instance, which handles it in its RPC loop
        assert "is_connected_to_shared_instance" in source and f'"manage": "{action}_interface"' in source
        assert f'path == "{action}_interface"' in _source(RNS.Reticulum.rpc_loop)
    # Result meanings: True done, None no such interface/entry, False refused
    detach = _source(RNS.Reticulum._detach_interface)
    assert "return None" in detach and "return True" in detach
    attach = _source(RNS.Reticulum._attach_interface)
    assert "force_attach=True" in attach and "return None" in attach
    assert "self.configpath" in attach
    assert isinstance(RNS.Reticulum.configpath, str)


def test_interface_stats_fields():
    stats = _source(RNS.Reticulum.get_interface_stats)
    for field in ('"short_name"', '"type"', '"status"', '"rxb"', '"txb"', '"interfaces"'):
        assert f"ifstats[{field}]" in stats or f"stats[{field}]" in stats, field
    assert '"get": "interface_stats"' in stats


def test_lxmf_api():
    assert LXMF.display_name_from_app_data(umsgpack.packb([b"Alice", 8])) == "Alice"
    for name in ("FIELD_IMAGE", "FIELD_FILE_ATTACHMENTS"):
        assert isinstance(getattr(LXMF, name), int)
    # Wire values other clients use; Retchat falls back to them if LXMF lacks the names
    assert (LXMF.FIELD_REPLY_TO, LXMF.FIELD_REPLY_QUOTE, LXMF.FIELD_REACTION) == (0x30, 0x31, 0x40)
    assert (LXMF.REACTION_TO, LXMF.REACTION_CONTENT) == (0x00, 0x01)
    for state in ("GENERATING", "OUTBOUND", "SENDING", "SENT", "DELIVERED", "FAILED", "REJECTED", "CANCELLED"):
        assert isinstance(getattr(LXMF.LXMessage, state), int)


def test_nomadnet_util():
    from nomadnet.util import STRIP_CONTROL_RE  # micron.py
    assert STRIP_CONTROL_RE.sub("", "a\x07b") == "ab"
