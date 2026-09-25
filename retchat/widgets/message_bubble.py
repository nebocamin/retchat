"""Message bubble widget, used as a recyclable row of the chat Gtk.ListView."""

import datetime
import functools
import os
from typing import Callable, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from retchat.models import MessageItem
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


class MessageBubble(Gtk.Box):
    """A chat bubble that can be re-bound to different MessageItems.

    The widget tree is built once; ``bind()`` fills it with the data of an item
    and ``unbind()`` releases it again, so Gtk.ListView can recycle rows.
    """

    def __init__(self, on_image_clicked: Optional[Callable[[MessageItem], None]] = None):
        super().__init__()
        self.add_css_class("message-row")
        self._on_image_clicked = on_image_clicked
        self._item: Optional[MessageItem] = None
        self._state_handler = 0

        self.bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.bubble.add_css_class("message-bubble")
        self.append(self.bubble)

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

        self._bind_image(item)
        has_image = self.picture.get_visible() or self.image_error.get_visible()
        for f in item.files:
            self.files_box.append(AttachmentRow(f["path"], f["name"], f["size"]))
        self.files_box.set_visible(bool(item.files))

        classes = ["message-bubble", "outgoing" if outgoing else "incoming"]
        if has_image:
            classes.append("has-image")
        self.bubble.set_css_classes(classes)

        text = item.content or ("" if has_image or item.files else "[Leere Nachricht]")
        self.label.set_text(text)
        self.label.set_visible(bool(text))

        time_str = self._format_time(item.timestamp)
        if not outgoing and item.hops > 0:
            time_str = f"{time_str} · {item.hops} Hop{'s' if item.hops > 1 else ''}"
        self.time_label.set_text(time_str)

        self._state_handler = item.connect("notify::state", lambda *_: self._update_state())
        self._update_state()

    def unbind(self):
        if self._item is not None and self._state_handler:
            self._item.disconnect(self._state_handler)
        self._item = None
        self._state_handler = 0
        self.picture.set_paintable(None)
        while child := self.files_box.get_first_child():
            self.files_box.remove(child)

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

    @staticmethod
    def _format_time(timestamp: float) -> str:
        if not timestamp:
            return ""
        dt = datetime.datetime.fromtimestamp(timestamp)
        if dt.date() == datetime.date.today():
            return dt.strftime("%H:%M")
        return dt.strftime("%d.%m. %H:%M")
