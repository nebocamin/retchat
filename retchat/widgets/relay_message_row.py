"""Recyclable row for the relay chat Gtk.ListView (messages, /me actions, notices)."""

import datetime
from html import escape
from typing import Optional

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango

from retchat.models import RelayMessageItem

NICK_COLORS = [
    "#3584e4",  # blue
    "#33d17a",  # green
    "#e5a50a",  # yellow/gold
    "#e66100",  # orange
    "#9141ac",  # purple
    "#1c71d8",  # dark blue
    "#26a269",  # dark green
    "#c061cb",  # magenta
    "#f66151",  # red
]


def get_nick_color(nick: str) -> str:
    if not nick:
        return NICK_COLORS[0]
    return NICK_COLORS[sum(ord(c) for c in nick) % len(NICK_COLORS)]


def _format_time(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M")


class RelayMessageRow(Gtk.Box):
    """Shows one RelayMessageItem as bubble, /me action or centered notice.

    All three variants are built once; ``bind()`` shows the matching one.
    """

    def __init__(self):
        super().__init__()
        self.add_css_class("message-row")
        self._item: Optional[RelayMessageItem] = None

        # Variant 1: system notice (joined/parted/notice)
        self.notice_label = Gtk.Label(
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            justify=Gtk.Justification.CENTER,
        )
        self.notice_label.add_css_class("relay-notice")
        self.append(self.notice_label)

        # Variant 2: /me action
        self.action_label = Gtk.Label(wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0.0)
        self.action_label.add_css_class("relay-action")
        self.append(self.action_label)

        # Variant 3: regular message bubble
        self.bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.nick_label = Gtk.Label(xalign=0.0, ellipsize=Pango.EllipsizeMode.END)
        self.nick_label.add_css_class("relay-nick")
        self.bubble.append(self.nick_label)

        self.text_label = Gtk.Label(
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
            xalign=0.0,
        )
        self.text_label.add_css_class("message-text")
        self.bubble.append(self.text_label)

        self.time_label = Gtk.Label(halign=Gtk.Align.END)
        self.time_label.add_css_class("message-time")
        self.bubble.append(self.time_label)
        self.append(self.bubble)

    def bind(self, item: RelayMessageItem):
        self._item = item
        time_str = _format_time(item.timestamp)
        nick = escape(item.nick)
        color = get_nick_color(item.nick)

        variant = "notice" if item.is_system else ("action" if item.kind == "action" else "msg")
        self.notice_label.set_visible(variant == "notice")
        self.action_label.set_visible(variant == "action")
        self.bubble.set_visible(variant == "msg")

        if variant == "notice":
            self.set_halign(Gtk.Align.CENTER)
            self.notice_label.set_text(f"{item.text} · {time_str}" if time_str else item.text)
        elif variant == "action":
            self.set_halign(Gtk.Align.END if item.is_me else Gtk.Align.START)
            self.action_label.set_markup(
                f"<i>* <span foreground='{color}' weight='bold'>{nick}</span> {escape(item.text)}</i>"
                f"  <span alpha='60%' size='small'>{time_str}</span>"
            )
        else:
            self.set_halign(Gtk.Align.END if item.is_me else Gtk.Align.START)
            self.bubble.set_css_classes(["message-bubble", "outgoing" if item.is_me else "incoming"])
            self.nick_label.set_visible(not item.is_me)
            self.nick_label.set_markup(f"<span foreground='{color}'>{nick}</span>")
            self.text_label.set_text(item.text)
            self.time_label.set_text(time_str)

    def unbind(self):
        self._item = None
