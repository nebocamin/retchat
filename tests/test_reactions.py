"""Tests for emoji reactions (LXMF FIELD_REACTION): wire format, storage, filtering."""

import os
import types

import LXMF
import pytest
import RNS
from nomadnet import Conversation

from retchat.database import Database
from retchat.reticulum_service import (
    FIELD_REACTION,
    FIELD_REACTION_LEGACY,
    REACTION_CONTENT,
    REACTION_TO,
    ReticulumService,
    parse_reaction,
    reaction_fields,
    sanitize_reaction,
    summarize_reactions,
)

TARGET = "ab" * 32
OWN = "11" * 16
PEER = "22" * 16


# --- Wire format ------------------------------------------------------------------

def test_reaction_fields_follow_lxmf_standard():
    fields = reaction_fields(TARGET, "👍")
    assert FIELD_REACTION == 0x40
    assert fields == {0x40: {0x00: bytes.fromhex(TARGET), 0x01: "👍".encode()}}


def _packed_roundtrip(fields):
    """Pack an LXMessage like a sending client and unpack it like the receiver."""
    sender, receiver = RNS.Identity(), RNS.Identity()
    src = RNS.Destination(sender, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
    dst = RNS.Destination(receiver, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
    lxm = LXMF.LXMessage(dst, src, "", fields=fields, desired_method=LXMF.LXMessage.DIRECT)
    lxm.pack()
    return LXMF.LXMessage.unpack_from_bytes(lxm.packed)


def test_reaction_survives_lxmf_packing():
    received = _packed_roundtrip(reaction_fields(TARGET, "❤️"))
    assert parse_reaction(received.fields) == (TARGET, "❤️")


def test_legacy_columba_meshchatx_format_survives_packing():
    received = _packed_roundtrip(
        {FIELD_REACTION_LEGACY: {"reaction_to": TARGET, "emoji": "😂", "sender": "ff" * 16}})
    assert parse_reaction(received.fields) == (TARGET, "😂")


@pytest.mark.parametrize("fields", [
    None,
    {},
    {LXMF.FIELD_IMAGE: ["webp", b"..."]},
    {FIELD_REACTION: {REACTION_TO: b"short", REACTION_CONTENT: b"x"}},
    {FIELD_REACTION: "not a dict"},
    {FIELD_REACTION_LEGACY: {"something": "else"}},
])
def test_non_reactions(fields):
    assert parse_reaction(fields) is None


def test_reaction_with_bad_content_is_still_a_reaction():
    fields = {FIELD_REACTION: {REACTION_TO: bytes.fromhex(TARGET), REACTION_CONTENT: b"\xff\xfe"}}
    assert parse_reaction(fields) == (TARGET, None)


@pytest.mark.parametrize("value, expected", [
    ("👍", "👍"),
    (" 👍 ".encode(), "👍"),
    ("👨‍👩‍👧‍👦", "👨‍👩‍👧‍👦"),  # ZWJ sequence
    ("🏴󠁧󠁢󠁳󠁣󠁴󠁿", "🏴󠁧󠁢󠁳󠁣󠁴󠁿"),  # subdivision flag (tag characters)
    ("👍🏽", "👍🏽"),  # skin tone
    ("", None),
    ("a\nb", None),
    ("\x07", None),
    ("x" * 40, None),
    (b"\xff", None),
    (42, None),
])
def test_sanitize_reaction(value, expected):
    assert sanitize_reaction(value) == expected


# --- Aggregation and storage --------------------------------------------------------

def test_summarize_counts_each_sender_once_and_keeps_order():
    rows = [
        {"target_hash": TARGET, "emoji": "👍", "sender_hash": PEER},
        {"target_hash": TARGET, "emoji": "❤️", "sender_hash": OWN},
        {"target_hash": TARGET, "emoji": "👍", "sender_hash": OWN},
        {"target_hash": TARGET, "emoji": "👍", "sender_hash": PEER},  # duplicate message
    ]
    assert summarize_reactions(rows, OWN) == {TARGET: [
        {"emoji": "👍", "count": 2, "mine": True},
        {"emoji": "❤️", "count": 1, "mine": True},
    ]}


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def test_database_reactions(db):
    db.add_reaction("01" * 32, PEER, TARGET, PEER, "👍", 1.0)
    db.add_reaction("01" * 32, PEER, TARGET, PEER, "👍", 1.0)  # delivered twice
    db.add_reaction("02" * 32, PEER, TARGET, PEER, "", 2.0)  # rejected content
    db.add_reaction("03" * 32, "33" * 16, TARGET, "33" * 16, "😂", 3.0)  # other chat

    assert [r["emoji"] for r in db.get_reactions(PEER)] == ["👍"]
    assert db.get_reaction_hashes() == {"01" * 32, "02" * 32, "03" * 32}
    assert db.has_reaction(PEER, TARGET, PEER, "👍")
    assert not db.has_reaction(PEER, TARGET, OWN, "👍")

    db.delete_conversation(PEER)
    assert db.get_reactions(PEER) == []
    assert db.get_reaction_hashes() == {"03" * 32}


# --- Service logic (without a running Reticulum instance) -----------------------------

class FakeMessage:
    """Enough of nomadnet's ConversationMessage."""

    def __init__(self, msg_hash, content="", fields=None, source=PEER, attachments=False):
        self._hash = bytes.fromhex(msg_hash)
        self._content = content
        self._fields = fields or {}
        self._cached_source_hash = bytes.fromhex(source)
        self._attachments = attachments
        self.loads = 0
        self.lxm = None
        self.sort_timestamp = 5.0

    def get_hash(self):
        return self._hash

    def get_content(self):
        return self._content

    def has_attachments(self):
        return self._attachments

    def get_fields(self):
        self.loads += 1
        return self._fields

    def get_timestamp(self):
        return 5.0


@pytest.fixture
def service(db, tmp_path):
    svc = ReticulumService.__new__(ReticulumService)
    svc.db = db
    svc._reaction_hashes = set()
    svc._plain_empty_hashes = set()
    svc.app = types.SimpleNamespace(conversationpath=str(tmp_path),
                                    lxmf_destination=types.SimpleNamespace(hash=bytes.fromhex(OWN)))
    return svc


def test_stored_reaction_messages_are_detected_and_hidden(service):
    old_reaction = FakeMessage("0a" * 32, fields=reaction_fields(TARGET, "🎉"))
    text = FakeMessage("0b" * 32, content="hello")
    empty = FakeMessage("0c" * 32)

    assert service._is_reaction_message(old_reaction, PEER)
    assert not service._is_reaction_message(text, PEER)
    assert not service._is_reaction_message(empty, PEER)
    assert text.loads == 0  # messages with text are never read from disk
    assert not service._is_reaction_message(empty, PEER)
    assert empty.loads == 1  # checked once only

    assert service.get_reactions(PEER, TARGET) == [{"emoji": "🎉", "count": 1, "mine": False}]


def test_inbound_reaction_is_registered(service):
    received = _packed_roundtrip(reaction_fields(TARGET, "👍"))
    assert service._register_inbound_reaction(received) == (TARGET, "👍")
    assert received.hash.hex() in service._reaction_hashes
    source = received.source_hash.hex()
    assert service.get_reactions(source, TARGET) == [{"emoji": "👍", "count": 1, "mine": False}]

    plain = _packed_roundtrip({})
    assert service._register_inbound_reaction(plain) is None


def test_restore_unread(service, tmp_path):
    src = bytes.fromhex(PEER)
    os.makedirs(tmp_path / PEER)
    unread_file = tmp_path / PEER / "unread"
    try:
        Conversation.unread_conversations[src] = 1
        unread_file.write_text("1")
        service._restore_unread(src, None)
        assert src not in Conversation.unread_conversations
        assert not unread_file.exists()

        Conversation.unread_conversations[src] = 4
        service._restore_unread(src, 3)
        assert Conversation.unread_conversations[src] == 3
        assert unread_file.read_text() == "3"
    finally:
        Conversation.unread_conversations.pop(src, None)


class FakeConversation:
    def __init__(self):
        self.sent = []
        self.messages = []

    def send(self, content="", title="", fields=None):
        self.sent.append((content, fields))
        self.messages.append(FakeMessage(f"{len(self.sent):064x}", fields=fields, source=OWN))
        return True


def test_send_reaction(service):
    conv = FakeConversation()
    service.conversation = lambda dest: conv
    service.peer_known = lambda dest: True

    result = service.send_reaction(PEER, TARGET, "👍")
    assert result == [{"emoji": "👍", "count": 1, "mine": True}]
    assert conv.sent == [("", reaction_fields(TARGET, "👍"))]
    assert f"{1:064x}" in service._reaction_hashes  # hidden in the chat

    # Same emoji again: nothing is sent
    service.send_reaction(PEER, TARGET, "👍")
    assert len(conv.sent) == 1

    with pytest.raises(ValueError):
        service.send_reaction(PEER, "out_1234", "👍")  # queued message, no LXMF hash
    with pytest.raises(ValueError):
        service.send_reaction(PEER, TARGET, "not an emoji at all, too long")
