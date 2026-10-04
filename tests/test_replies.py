"""Replies (LXMF FIELD_REPLY_TO / FIELD_REPLY_QUOTE): wire format, sending, loading."""

import types

import pytest

from retchat.database import Database
from retchat.reticulum_service import (
    FIELD_REACTION_LEGACY,
    FIELD_REPLY_QUOTE,
    FIELD_REPLY_TO,
    MAX_REPLY_QUOTE,
    ReticulumService,
    parse_reply,
    reply_fields,
)
from tests.test_reactions import OWN, PEER, TARGET, FakeMessage, _packed_roundtrip


# --- Wire format ------------------------------------------------------------------

def test_reply_fields_follow_lxmf_standard():
    assert (FIELD_REPLY_TO, FIELD_REPLY_QUOTE) == (0x30, 0x31)
    assert reply_fields(TARGET, "Guten Morgen") == {0x30: bytes.fromhex(TARGET), 0x31: "Guten Morgen".encode()}
    assert reply_fields(TARGET, "") == {0x30: bytes.fromhex(TARGET)}


def test_quote_is_shortened_and_cleaned():
    quote = reply_fields(TARGET, "x" * 500 + "\x07")[0x31].decode()
    assert len(quote) == MAX_REPLY_QUOTE and quote.endswith("…")
    assert reply_fields(TARGET, "a\x00b\nc")[0x31] == b"ab\nc"


def test_reply_survives_lxmf_packing():
    received = _packed_roundtrip(reply_fields(TARGET, "ok hier auch"))
    assert parse_reply(received.fields) == (TARGET, "ok hier auch")


@pytest.mark.parametrize("fields, expected", [
    # MeshChatX: bytes, as recorded from a real message
    ({0x30: bytes.fromhex(TARGET), 0x31: b"[phoshy] alles da, schoen"}, (TARGET, "[phoshy] alles da, schoen")),
    ({0x30: TARGET.upper()}, (TARGET, "")),               # hex string
    ({0x30: bytes.fromhex(TARGET), 0x31: "text"}, (TARGET, "text")),
    ({FIELD_REACTION_LEGACY: {"reply_to": TARGET}}, (TARGET, "")),  # older Columba
    ({FIELD_REACTION_LEGACY: {b"reply_to": TARGET.encode()}}, (TARGET, "")),
    ({0x30: b"\x01" * 16}, None),                          # not a message hash
    ({0x31: b"quote without target"}, None),
    ({}, None),
    (None, None),
    ("garbage", None),
])
def test_parse_reply(fields, expected):
    assert parse_reply(fields) == expected


def test_bad_utf8_in_quote_is_replaced():
    assert parse_reply({0x30: bytes.fromhex(TARGET), 0x31: b"ok \xff"}) == (TARGET, "ok \ufffd")


# --- Service ---------------------------------------------------------------------------

class FakeConversation:
    def __init__(self, messages=()):
        self.sent = []
        self.messages = list(messages)

    def send(self, content="", title="", fields=None):
        self.sent.append((content, fields))
        self.messages.append(FakeMessage(f"{len(self.sent):064x}", content=content, fields=fields, source=OWN))
        return True

    def scan_storage(self):
        pass


class Msg(FakeMessage):
    def get_title(self):
        return ""

    def get_state(self):
        return 0x08  # LXMessage.DELIVERED

    def get_fields(self):
        self.loads += 1
        return self._fields


@pytest.fixture
def service(tmp_path):
    svc = ReticulumService.__new__(ReticulumService)
    svc.db = Database(str(tmp_path / "test.db"))
    svc._reaction_hashes = set()
    svc._plain_empty_hashes = set()
    svc._pending = {}
    svc.app = types.SimpleNamespace(conversationpath=str(tmp_path), attachmentpath=str(tmp_path / "att"),
                                    lxmf_destination=types.SimpleNamespace(hash=bytes.fromhex(OWN)))
    return svc


def test_send_reply_quotes_the_original(service, monkeypatch):
    conv = FakeConversation([Msg(TARGET, content="weisst du schon, wie man nen reticulum r?")])
    service.conversation = lambda dest: conv
    service.peer_known = lambda dest: True
    monkeypatch.setattr("retchat.reticulum_service.RNS.Transport.hops_to", lambda h: 1)

    result = service.send_message(PEER, "ja", reply_to=TARGET.upper())
    assert conv.sent == [("ja", reply_fields(TARGET, "weisst du schon, wie man nen reticulum r?"))]
    assert result["reply_to"] == TARGET
    assert result["reply_quote"] == "weisst du schon, wie man nen reticulum r?"


def test_reply_to_unknown_message_sends_without_quote(service, monkeypatch):
    conv = FakeConversation()
    service.conversation = lambda dest: conv
    service.peer_known = lambda dest: True
    monkeypatch.setattr("retchat.reticulum_service.RNS.Transport.hops_to", lambda h: 1)
    service.send_message(PEER, "ja", reply_to=TARGET)
    assert conv.sent == [("ja", {FIELD_REPLY_TO: bytes.fromhex(TARGET)})]


def test_reply_to_invalid_hash_is_refused(service):
    with pytest.raises(ValueError):
        service.send_message(PEER, "ja", reply_to="out_1234")


def test_queued_reply_keeps_its_target(service, monkeypatch):
    conv = FakeConversation([Msg(TARGET, content="hallo")])
    service.conversation = lambda dest: conv
    monkeypatch.setattr("retchat.reticulum_service.RNS.Transport.hops_to", lambda h: 1)
    monkeypatch.setattr("retchat.reticulum_service.GLib.idle_add", lambda *a: None)
    service._schedule_conversations_changed = lambda: None

    service.peer_known = lambda dest: False
    monkeypatch.setattr("retchat.reticulum_service.Conversation.query_for_peer", lambda h: None)
    queued = service.send_message(PEER, "später", reply_to=TARGET)
    assert queued["message_hash"].startswith("out_") and conv.sent == []

    service.peer_known = lambda dest: True
    assert service.flush_pending() == [PEER]
    assert conv.sent == [("später", reply_fields(TARGET, "hallo"))]


def test_get_messages_reports_replies(service):
    original = Msg(TARGET, content="frage")
    reply = Msg("cd" * 32, content="antwort", fields=reply_fields(TARGET, "frage"))
    plain = Msg("ef" * 32, content="noch was")
    conv = FakeConversation([original, reply, plain])
    service.conversation = lambda dest: conv

    by_hash = {m["message_hash"]: m for m in service.get_messages(PEER)}
    assert (by_hash["cd" * 32]["reply_to"], by_hash["cd" * 32]["reply_quote"]) == (TARGET, "frage")
    assert by_hash[TARGET]["reply_to"] is None and by_hash[TARGET]["reply_quote"] == ""
