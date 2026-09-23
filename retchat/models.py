"""GObject data models used by list-based widgets."""

from typing import Any, Dict, Optional

from gi.repository import GObject


class MessageItem(GObject.Object):
    """A single chat message, stored in a Gio.ListStore and bound to a MessageBubble.

    Only ``state`` changes during the lifetime of a message; bubbles listen to
    ``notify::state`` to update the delivery indicator.
    """

    __gtype_name__ = "RetchatMessageItem"

    message_hash = GObject.Property(type=str, default="")
    is_outgoing = GObject.Property(type=bool, default=False)
    content = GObject.Property(type=str, default="")
    timestamp = GObject.Property(type=float, default=0.0)
    hops = GObject.Property(type=int, default=0)
    state = GObject.Property(type=int, default=0)
    image_path = GObject.Property(type=str, default=None)
    image_name = GObject.Property(type=str, default=None)
    image_size = GObject.Property(type=GObject.TYPE_INT64, default=-1)

    def __init__(self, data: Dict[str, Any]):
        super().__init__(
            message_hash=data["message_hash"],
            is_outgoing=bool(data.get("is_outgoing", False)),
            content=(data.get("content") or "").strip(),
            timestamp=float(data.get("timestamp") or 0.0),
            hops=int(data.get("hops") or 0),
            state=int(data.get("state") or 0),
            image_path=data.get("image_path"),
            image_name=data.get("image_name"),
            image_size=int(data["image_size"]) if data.get("image_size") is not None else -1,
        )

    @property
    def image_size_or_none(self) -> Optional[int]:
        return self.image_size if self.image_size >= 0 else None


class RelayMessageItem(GObject.Object):
    """A single Reticulum Relay Chat (RRC) room event, bound to a RelayMessageRow.

    ``kind`` is "msg" for regular messages, "action" for /me, and
    "notice" / "joined" / "parted" for system events.
    """

    __gtype_name__ = "RetchatRelayMessageItem"

    SYSTEM_KINDS = ("notice", "joined", "parted")

    kind = GObject.Property(type=str, default="msg")
    nick = GObject.Property(type=str, default="")
    src = GObject.Property(type=str, default="")
    text = GObject.Property(type=str, default="")
    timestamp = GObject.Property(type=float, default=0.0)
    is_me = GObject.Property(type=bool, default=False)

    def __init__(self, data: Dict[str, Any]):
        super().__init__(
            kind=data.get("kind") or "msg",
            nick=data.get("nick") or "System",
            src=data.get("src") or "",
            text=data.get("text") or "",
            timestamp=float(data.get("timestamp") or 0.0),
            is_me=bool(data.get("is_me", False)),
        )

    @property
    def is_system(self) -> bool:
        return self.kind in self.SYSTEM_KINDS or self.nick == "System"

    @property
    def dedup_key(self) -> tuple:
        """Identity used to drop duplicates (echoes of own messages, reloads)."""
        return (self.kind, self.src, self.text, round(self.timestamp))
