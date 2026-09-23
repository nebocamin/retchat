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
