"""Message bubble widget, used as a recyclable row of the chat Gtk.ListView."""

import datetime
import functools
import os
import sys
from typing import Callable, Optional, Tuple

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Graphene', '1.0')
from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gtk, Pango

from retchat.models import MessageItem
from retchat.nomadnet_pages import find_urls
from retchat.widgets.attachment_row import AttachmentRow
from retchat.reticulum_service import STATE_DELIVERED, STATE_FAILED, STATE_SENDING, STATE_SENT

THUMB_MAX_WIDTH = 260
THUMB_MAX_HEIGHT = 320
THUMB_MIN_WIDTH = 80
THUMB_MIN_HEIGHT = 60
# Thumbnails are decoded at twice the display size, sharp on HiDPI screens.
# A decoded thumbnail is at most 520x640 RGBA (1.3 MB) instead of e.g. 45 MB
# for a full 4000x3000 photo.
THUMB_DECODE_SCALE = 2
THUMB_CACHE_SIZE = 32

_TOUCH_EVENTS = (Gdk.EventType.TOUCH_BEGIN, Gdk.EventType.TOUCH_UPDATE,
                 Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL)
_POINTER_EVENTS = (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.MOTION_NOTIFY)
_TOUCH_SOURCES = (Gdk.InputSource.TOUCHSCREEN, Gdk.InputSource.PEN)
# RETCHAT_DEBUG_INPUT=1: log the input events reaching message texts (stderr)
_DEBUG_INPUT = bool(os.environ.get("RETCHAT_DEBUG_INPUT"))


def is_touch_input(event: Gdk.Event) -> bool:
    """Whether a pointer or touch event comes from a finger (or pen) instead of a mouse.

    Depending on device and compositor, touches arrive as touch events, as
    pointer events emulated from them, or from a touchscreen (or pen-like
    panel) device as plain pointer events.
    """
    event_type = event.get_event_type()
    if event_type in _TOUCH_EVENTS:
        return True
    if event_type not in _POINTER_EVENTS:
        return False
    try:
        if event.get_pointer_emulated():
            return True
    except (AttributeError, TypeError):
        pass
    device = event.get_device()
    return device is not None and device.get_source() in _TOUCH_SOURCES


def _describe(event: Gdk.Event) -> str:
    device = event.get_device()
    source = device.get_source().value_nick if device is not None else "?"
    try:
        emulated = event.get_pointer_emulated()
    except (AttributeError, TypeError):
        emulated = "?"
    return f"{event.get_event_type().value_nick} source={source} emulated={emulated}"


@functools.lru_cache(maxsize=THUMB_CACHE_SIZE)
def load_thumbnail(path: str, _mtime: float) -> Optional[Gdk.Texture]:
    """Decode an image at thumbnail size (the mtime only invalidates the cache)."""
    max_w, max_h = THUMB_MAX_WIDTH * THUMB_DECODE_SCALE, THUMB_MAX_HEIGHT * THUMB_DECODE_SCALE
    try:
        _fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(path)
        if w <= max_w and h <= max_h:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)  # never upscale small images
        else:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, max_w, max_h, True)
        pixbuf = pixbuf.apply_embedded_orientation() or pixbuf  # EXIF rotation of phone photos
    except (GLib.Error, TypeError):
        return None
    memory_format = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pixbuf.get_width(), pixbuf.get_height(), memory_format,
                                 pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())


def _thumbnail_size(texture: Gdk.Texture) -> tuple[int, int]:
    w, h = max(1, texture.get_width()), max(1, texture.get_height())
    scale = min(THUMB_MAX_WIDTH / w, THUMB_MAX_HEIGHT / h, 1.0)
    return max(THUMB_MIN_WIDTH, int(w * scale)), max(THUMB_MIN_HEIGHT, int(h * scale))


def linkify(text: str) -> Optional[str]:
    """Pango markup with clickable nomadnetwork:// links, None if there are none."""
    urls = find_urls(text)
    if not urls:
        return None
    out, pos = [], 0
    for start, end, url in urls:
        out.append(GLib.markup_escape_text(text[pos:start]))
        escaped = GLib.markup_escape_text(url).replace('"', "&quot;")
        out.append(f'<a href="{escaped}" title="NomadNet-Seite öffnen">{GLib.markup_escape_text(url)}</a>')
        pos = end
    out.append(GLib.markup_escape_text(text[pos:]))
    return "".join(out)


def can_reply(item: MessageItem) -> bool:
    """Replies need the message's LXMF hash (not yet known while queued)."""
    return len(item.message_hash) == 64


def can_react(item: MessageItem) -> bool:
    """Reactions are possible on received messages with a real LXMF hash."""
    return not item.is_outgoing and can_reply(item)


class MessageBubble(Gtk.Box):
    """A chat bubble that can be re-bound to different MessageItems.

    The widget tree is built once; ``bind()`` fills it with the data of an item
    and ``unbind()`` releases it again, so Gtk.ListView can recycle rows.

    The message menu (quick reactions for received messages, reply, copy)
    opens with the smiley button next to the bubble (shown on hover), a
    right click or a long press on the bubble; these call
    ``on_menu_requested(item, widget, x, y)`` to open it pointing at (x, y)
    in ``widget``. The reply button next to it calls
    ``on_reply_requested(item)``. Reaction chips below the bubble react with
    the same emoji through the ``chat.react`` action.

    A reply shows the message it refers to above the text, as returned by
    ``resolve_reply(item)``: (author, text, the original is in the chat).
    Clicking it calls ``on_quote_clicked(item)``.

    Touch: a selectable Gtk.Label claims every touch on press (to start a
    selection), which cancels the gestures of its ancestors: the long press
    that opens the menu and the scrolled window's drag. So touch input on
    the text (see is_touch_input) is kept from the label, except on links,
    which stay tappable; mouse input is unaffected, text can still be
    selected with it.
    """

    def __init__(
        self,
        on_image_clicked: Optional[Callable[[MessageItem], None]] = None,
        on_menu_requested: Optional[Callable[[MessageItem, Gtk.Widget, float, float], None]] = None,
        peer_name: Optional[Callable[[], str]] = None,
        on_reply_requested: Optional[Callable[[MessageItem], None]] = None,
        resolve_reply: Optional[Callable[[MessageItem], Tuple[str, str, bool]]] = None,
        on_quote_clicked: Optional[Callable[[MessageItem], None]] = None,
    ):
        super().__init__(spacing=4)
        self.add_css_class("message-row")
        self._on_image_clicked = on_image_clicked
        self._on_menu_requested = on_menu_requested
        self._on_reply_requested = on_reply_requested
        self._resolve_reply = resolve_reply
        self._on_quote_clicked = on_quote_clicked
        self._peer_name = peer_name
        self._item: Optional[MessageItem] = None
        self._state_handler = 0
        self._reactions_handler = 0
        self._highlight_handler = 0
        self._quote_jumpable = False

        self.column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.append(self.column)

        self.bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.bubble.add_css_class("message-bubble")
        self.column.append(self.bubble)

        # Quoted message of a reply
        self.quote_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=False)
        self.quote_box.add_css_class("reply-quote")
        self.quote_author = Gtk.Label(xalign=0.0, ellipsize=Pango.EllipsizeMode.END)
        self.quote_author.add_css_class("reply-author")
        self.quote_box.append(self.quote_author)
        self.quote_text = Gtk.Label(xalign=0.0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                    lines=2, ellipsize=Pango.EllipsizeMode.END)
        self.quote_text.add_css_class("reply-text")
        self.quote_box.append(self.quote_text)
        quote_click = Gtk.GestureClick()
        quote_click.connect("released", self._on_quote_released)
        self.quote_box.add_controller(quote_click)
        self.bubble.append(self.quote_box)

        # Reaction chips, overlapping the lower edge of the bubble
        self.reactions_box = Gtk.Box(spacing=4, visible=False)
        self.reactions_box.add_css_class("message-reactions")
        self.column.append(self.reactions_box)

        # Hover buttons; on the side facing the middle of the chat
        self.actions_box = Gtk.Box(valign=Gtk.Align.CENTER)
        self.reply_btn = Gtk.Button(icon_name="mail-reply-sender-symbolic", tooltip_text="Antworten")
        self.react_btn = Gtk.Button(icon_name="face-smile-symbolic", tooltip_text="Reagieren")
        for btn in (self.reply_btn, self.react_btn):
            for css in ("flat", "circular", "react-button"):
                btn.add_css_class(css)
            self.actions_box.append(btn)
        self.reply_btn.connect("clicked", self._on_reply_clicked)
        self.react_btn.connect("clicked", self._on_react_clicked)
        self.append(self.actions_box)

        # Right click (mouse) or long press (touch) opens the message menu.
        # Capture phase: the text label would otherwise show its own context
        # menu or start a selection.
        right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        right_click.connect("pressed", lambda g, _n, x, y: self._on_menu_gesture(g, x, y))
        self.bubble.add_controller(right_click)
        # Not touch_only: some touch panels report as pen; with a mouse the
        # text label claims the press first anyway.
        long_press = Gtk.GestureLongPress()
        long_press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        long_press.connect("pressed", self._on_menu_gesture)
        self.bubble.add_controller(long_press)

        # Image attachment
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.picture.set_overflow(Gtk.Overflow.HIDDEN)
        self.picture.set_halign(Gtk.Align.START)
        self.picture.add_css_class("message-image")
        self.picture.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        click = Gtk.GestureClick()
        click.connect("released", self._on_picture_released)
        self.picture.add_controller(click)
        self.bubble.append(self.picture)

        self.image_error = Gtk.Box(spacing=6)
        self.image_error.add_css_class("image-error")
        self.image_error.append(Gtk.Image(icon_name="image-missing-symbolic"))
        self.image_error.append(Gtk.Label(label="Bild konnte nicht geladen werden"))
        self.bubble.append(self.image_error)

        # Other attachments (documents, audio, further images)
        self.files_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.bubble.append(self.files_box)

        # Text
        self.label = Gtk.Label(
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
            xalign=0.0,
        )
        self.label.add_css_class("message-text")
        self.label.connect("activate-link", self._on_activate_link)
        touch_guard = Gtk.EventControllerLegacy()
        touch_guard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        touch_guard.connect("event", self._on_label_touch)
        self.label.add_controller(touch_guard)
        self._link_ranges: list = []  # (start, end) byte offsets of links in the label text
        self._touch_on_link = False
        self.bubble.append(self.label)

        # Footer: time, hops, delivery state
        footer = Gtk.Box(spacing=4, halign=Gtk.Align.END)
        footer.add_css_class("message-footer")
        self.time_label = Gtk.Label()
        self.time_label.add_css_class("message-time")
        footer.append(self.time_label)

        self.state_icon = Gtk.Image(pixel_size=12)
        self.state_icon.add_css_class("message-state")
        footer.append(self.state_icon)

        self.state_label = Gtk.Label()
        self.state_label.add_css_class("message-state")
        footer.append(self.state_label)
        self.bubble.append(footer)

    # --- Binding -------------------------------------------------------------

    def bind(self, item: MessageItem):
        self.unbind()
        self._item = item

        outgoing = item.is_outgoing
        self.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        self.set_css_classes(["message-row", "outgoing" if outgoing else "incoming"])
        self.reactions_box.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        self.react_btn.set_visible(can_react(item))
        self.reply_btn.set_visible(can_reply(item))
        if outgoing:
            self.reorder_child_after(self.actions_box, None)
        else:
            self.reorder_child_after(self.actions_box, self.column)

        self._bind_quote(item)

        self._bind_image(item)
        has_image = self.picture.get_visible() or self.image_error.get_visible()
        for f in item.files:
            self.files_box.append(AttachmentRow(f["path"], f["name"], f["size"]))
        self.files_box.set_visible(bool(item.files))

        classes = ["message-bubble", "outgoing" if outgoing else "incoming"]
        if has_image:
            classes.append("has-image")
        if item.highlighted:
            classes.append("highlighted")
        self.bubble.set_css_classes(classes)

        text = item.content or ("" if has_image or item.files else "[Leere Nachricht]")
        self._link_ranges = [(len(text[:start].encode()), len(text[:end].encode()))
                             for start, end, _url in find_urls(text)]
        markup = linkify(text)
        if markup is None:
            self.label.set_text(text)  # also turns markup parsing off again
        else:
            self.label.set_markup(markup)
        self.label.set_visible(bool(text))

        time_str = self._format_time(item.timestamp)
        if not outgoing and item.hops > 0:
            time_str = f"{time_str} · {item.hops} Hop{'s' if item.hops > 1 else ''}"
        self.time_label.set_text(time_str)

        self._state_handler = item.connect("notify::state", lambda *_: self._update_state())
        self._update_state()
        self._reactions_handler = item.connect("notify::reactions", lambda *_: self._update_reactions())
        self._update_reactions()
        self._highlight_handler = item.connect("notify::highlighted", lambda *_: self._update_highlight())

    def unbind(self):
        if self._item is not None:
            for handler in (self._state_handler, self._reactions_handler, self._highlight_handler):
                if handler:
                    self._item.disconnect(handler)
        self._item = None
        self._state_handler = 0
        self._reactions_handler = 0
        self._highlight_handler = 0
        self.picture.set_paintable(None)
        while child := self.files_box.get_first_child():
            self.files_box.remove(child)
        self._clear_reactions()

    def _bind_quote(self, item: MessageItem):
        if not item.reply_to or self._resolve_reply is None:
            self.quote_box.set_visible(False)
            self._quote_jumpable = False
            return
        author, text, jumpable = self._resolve_reply(item)
        self.quote_author.set_text(author)
        self.quote_text.set_text(text)
        self.quote_text.set_visible(bool(text))
        self._quote_jumpable = jumpable
        self.quote_box.set_cursor(Gdk.Cursor.new_from_name("pointer", None) if jumpable else None)
        self.quote_box.set_tooltip_text("Zur ursprünglichen Nachricht" if jumpable else None)
        self.quote_box.set_visible(True)

    def _update_highlight(self):
        if self._item is None:
            return
        if self._item.highlighted:
            self.bubble.add_css_class("highlighted")
        else:
            self.bubble.remove_css_class("highlighted")

    def _on_quote_released(self, gesture, _n_press, _x, _y):
        if self._item is not None and self._quote_jumpable and self._on_quote_clicked:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._on_quote_clicked(self._item)

    def _clear_reactions(self):
        while child := self.reactions_box.get_first_child():
            self.reactions_box.remove(child)

    def _update_reactions(self):
        self._clear_reactions()
        item = self._item
        reactions = (item.reactions or []) if item is not None else []
        self.reactions_box.set_visible(bool(reactions))
        if item is None:
            return
        reactable = can_react(item)
        for r in reactions:
            count = int(r.get("count") or 1)
            chip = Gtk.Button(label=r["emoji"] if count < 2 else f"{r['emoji']} {count}")
            chip.add_css_class("reaction-chip")
            if r.get("mine"):
                chip.add_css_class("mine")
            chip.set_tooltip_text(self._reaction_tooltip(r["emoji"], count, bool(r.get("mine"))))
            if reactable and not r.get("mine"):
                # Tap an emoji to react with it as well (action: no Python
                # reference from the chip back to this row).
                chip.set_action_name("chat.react")
                chip.set_action_target_value(GLib.Variant("(ss)", (item.message_hash, r["emoji"])))
            else:
                chip.set_can_target(False)
            self.reactions_box.append(chip)

    def _reaction_tooltip(self, emoji: str, count: int, mine: bool) -> str:
        peer = (self._peer_name() if self._peer_name else "") or "Kontakt"
        if mine:
            who = f"Du und {peer}" if count > 1 else "Du"
        else:
            who = peer
        return f"{who} {'hast' if who == 'Du' else 'hat' if count == 1 else 'haben'} mit {emoji} reagiert"

    def _bind_image(self, item: MessageItem):
        path = item.image_path
        if not path:
            self.picture.set_visible(False)
            self.image_error.set_visible(False)
            return

        try:
            texture = load_thumbnail(path, os.path.getmtime(path))
        except OSError:  # file missing
            texture = None
        self.picture.set_visible(texture is not None)
        self.image_error.set_visible(texture is None)
        if texture is not None:
            self.picture.set_paintable(texture)
            self.picture.set_size_request(*_thumbnail_size(texture))

    def _update_state(self):
        item = self._item
        if item is None or not item.is_outgoing:
            self.state_icon.set_visible(False)
            self.state_label.set_visible(False)
            return

        state = item.state
        icon, glyph, tooltip = {
            STATE_SENDING: ("document-open-recent-symbolic", None, "Wird gesendet"),
            STATE_SENT: (None, "✓", "Gesendet"),
            STATE_DELIVERED: (None, "✓✓", "Zugestellt"),
            STATE_FAILED: ("dialog-error-symbolic", None, "Fehlgeschlagen"),
        }.get(state, (None, None, None))

        self.state_icon.set_visible(icon is not None)
        if icon:
            self.state_icon.set_from_icon_name(icon)
        self.state_label.set_visible(glyph is not None)
        if glyph:
            self.state_label.set_text(glyph)

        for widget in (self.state_icon, self.state_label):
            widget.set_tooltip_text(tooltip)
            widget.remove_css_class("delivered")
            widget.remove_css_class("error")
            if state == STATE_DELIVERED:
                widget.add_css_class("delivered")
            elif state == STATE_FAILED:
                widget.add_css_class("error")

    # --- Helpers -------------------------------------------------------------

    def _on_picture_released(self, _gesture, _n_press, _x, _y):
        if self._item is not None and self._on_image_clicked:
            self._on_image_clicked(self._item)

    def _on_label_touch(self, _controller, event: Gdk.Event) -> bool:
        """Keep touch input from the text label (see class docstring); True = stop."""
        touch = is_touch_input(event)
        if touch and event.get_event_type() in (Gdk.EventType.TOUCH_BEGIN, Gdk.EventType.BUTTON_PRESS):
            self._touch_on_link = False
            if self._link_ranges:
                position = self._label_position(event)
                self._touch_on_link = position is not None and self.link_at(*position)
        stop = touch and not self._touch_on_link
        if _DEBUG_INPUT and event.get_event_type() not in (Gdk.EventType.MOTION_NOTIFY, Gdk.EventType.TOUCH_UPDATE):
            print(f"Retchat input: message text got {_describe(event)} -> "
                  f"{'kept from the label' if stop else 'passed to the label'}", file=sys.stderr, flush=True)
        return stop

    def _label_position(self, event: Gdk.Event) -> Optional[Tuple[float, float]]:
        native = self.label.get_native()
        found, x, y = event.get_position()
        if native is None or not found:
            return None
        tx, ty = native.get_surface_transform()
        ok, point = native.compute_point(self.label, Graphene.Point().init(x - tx, y - ty))
        return (point.x, point.y) if ok else None

    def link_at(self, x: float, y: float) -> bool:
        """Whether (x, y) in label coordinates is on a link."""
        if not self._link_ranges:
            return False
        ox, oy = self.label.get_layout_offsets()
        inside, index, _trailing = self.label.get_layout().xy_to_index(
            int((x - ox) * Pango.SCALE), int((y - oy) * Pango.SCALE))
        return inside and any(start <= index < end for start, end in self._link_ranges)

    def _on_activate_link(self, _label: Gtk.Label, uri: str) -> bool:
        # A window action: rows are recycled and must not reference the window.
        self.activate_action("win.open-nomadnet-url", GLib.Variant.new_string(uri))
        return True

    def _on_react_clicked(self, button: Gtk.Button):
        if self._item is not None and self._on_menu_requested:
            self._on_menu_requested(self._item, button, button.get_width() / 2, button.get_height() / 2)

    def _on_reply_clicked(self, _button: Gtk.Button):
        if self._item is not None and self._on_reply_requested:
            self._on_reply_requested(self._item)

    def _on_menu_gesture(self, gesture: Gtk.Gesture, x: float, y: float):
        if _DEBUG_INPUT:
            print(f"Retchat input: {type(gesture).__name__} on message -> menu", file=sys.stderr, flush=True)
        item = self._item
        if item is None or not self._on_menu_requested or not (can_reply(item) or item.content):
            return  # nothing to offer: keep the text label's context menu
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._on_menu_requested(item, self.bubble, x, y)

    @staticmethod
    def _format_time(timestamp: float) -> str:
        if not timestamp:
            return ""
        dt = datetime.datetime.fromtimestamp(timestamp)
        if dt.date() == datetime.date.today():
            return dt.strftime("%H:%M")
        return dt.strftime("%d.%m. %H:%M")
