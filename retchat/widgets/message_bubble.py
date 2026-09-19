"""Message bubble widget for chat view."""

import datetime
from typing import Any, Dict, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Pango, Adw

from retchat.reticulum_service import STATE_SENDING, STATE_SENT, STATE_DELIVERED, STATE_FAILED


class MessageBubble(Gtk.Box):
    def __init__(self, message_data: Dict[str, Any]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        self.message_data = message_data
        self.message_hash = message_data["message_hash"]
        self.is_outgoing = bool(message_data.get("is_outgoing", False))
        self.content = message_data.get("content", "")
        self.timestamp = message_data.get("timestamp", 0.0)
        self.state = message_data.get("state", STATE_SENDING)
        self.hops = message_data.get("hops", 0)

        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.set_margin_start(12)
        self.set_margin_end(12)

        # Bubble container
        self.bubble_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.bubble_box.set_size_request(80, -1)

        # Message text
        self.label = Gtk.Label(
            label=self.content,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
            xalign=0.0
        )
        self.label.add_css_class("message-text")
        self.bubble_box.append(self.label)

        # Meta footer (time, hops, status)
        self.meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.meta_box.set_halign(Gtk.Align.END)

        # Time label
        time_str = self._format_time(self.timestamp)
        if not self.is_outgoing and self.hops > 0:
            time_str = f"{time_str} • {self.hops} Hop{'s' if self.hops > 1 else ''}"
        self.time_label = Gtk.Label(label=time_str)
        self.time_label.add_css_class("message-time")
        self.meta_box.append(self.time_label)

        # Status icon for outgoing
        self.status_icon = None
        if self.is_outgoing:
            self.status_icon = Gtk.Label(label=self._get_state_symbol(self.state))
            self.status_icon.add_css_class("message-status")
            self.meta_box.append(self.status_icon)

        self.bubble_box.append(self.meta_box)

        # Alignment and styles
        if self.is_outgoing:
            self.set_halign(Gtk.Align.END)
            self.bubble_box.add_css_class("bubble-outgoing")
        else:
            self.set_halign(Gtk.Align.START)
            self.bubble_box.add_css_class("bubble-incoming")

        self.append(self.bubble_box)

    def _format_time(self, timestamp: float) -> str:
        if not timestamp:
            return ""
        try:
            dt = datetime.datetime.fromtimestamp(timestamp)
            return dt.strftime("%H:%M")
        except Exception:
            return ""

    def _get_state_symbol(self, state: int) -> str:
        if state == STATE_SENDING:
            return "🕒"
        elif state == STATE_SENT:
            return "✓"
        elif state == STATE_DELIVERED:
            return "✓✓"
        elif state == STATE_FAILED:
            return "❌"
        return ""

    def update_state(self, new_state: int):
        self.state = new_state
        if self.status_icon:
            self.status_icon.set_label(self._get_state_symbol(new_state))
            if new_state == STATE_FAILED:
                self.status_icon.add_css_class("error")
            elif new_state == STATE_DELIVERED:
                self.status_icon.add_css_class("delivered")
