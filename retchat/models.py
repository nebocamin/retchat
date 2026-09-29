"""GObject data models used by list-based widgets."""

from typing import Any, Dict, List, Optional

from gi.repository import GObject


class MessageItem(GObject.Object):
    """A single chat message, stored in a Gio.ListStore and bound to a MessageBubble.

    Only ``state`` and ``reactions`` change during the lifetime of a message;
    bubbles listen to ``notify::state`` and ``notify::reactions``.

    ``files`` lists attachments other than the inline image as
    ``{"path", "name", "size"}`` dicts (a plain attribute, it never changes).

    ``reactions`` is a list of ``{"emoji", "count", "mine"}`` dicts; assign a
    new list to change it (changing the list in place doesn't notify).
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
    reactions = GObject.Property(type=object)

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
            reactions=list(data.get("reactions") or []),
        )
        self.files: List[Dict[str, Any]] = list(data.get("files") or [])

    @property
    def image_size_or_none(self) -> Optional[int]:
        return self.image_size if self.image_size >= 0 else None
