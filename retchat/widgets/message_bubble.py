"""Message bubble widget for chat view."""

import datetime
import os
from typing import Any, Callable, Dict, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, Gio, Pango, Adw, GLib

from retchat.reticulum_service import STATE_SENDING, STATE_SENT, STATE_DELIVERED, STATE_FAILED


class MessageBubble(Gtk.Box):
    def __init__(
        self,
        message_data: Dict[str, Any],
        on_image_clicked: Optional[Callable[[str, Dict[str, Any]], None]] = None
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        self.message_data = message_data
        self.message_hash = message_data["message_hash"]
        self.is_outgoing = bool(message_data.get("is_outgoing", False))
        self.content = message_data.get("content", "")
        self.timestamp = message_data.get("timestamp", 0.0)
        self.state = message_data.get("state", STATE_SENDING)
        self.hops = message_data.get("hops", 0)
        self.image_path = message_data.get("image_path")
        self.image_name = message_data.get("image_name")
        self.image_size = message_data.get("image_size")
        self.on_image_clicked = on_image_clicked

        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.set_margin_start(12)
        self.set_margin_end(12)

        has_image = bool(self.image_path and os.path.isfile(self.image_path))
        has_text = bool(self.content and self.content.strip())

        # Bubble container
        self.bubble_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.bubble_box.set_size_request(80, -1)
        if has_image:
            self.bubble_box.add_css_class("bubble-has-image")

        # 1. Image widget
        if has_image:
            img_widget = self._create_image_widget()
            if img_widget:
                self.bubble_box.append(img_widget)

        # 2. Message text (if text is present or if neither text nor image exists)
        if has_text or not has_image:
            display_text = self.content if has_text else ("[Bild]" if has_image else "")
            if display_text:
                self.label = Gtk.Label(
                    label=display_text,
                    wrap=True,
                    wrap_mode=Pango.WrapMode.WORD_CHAR,
                    selectable=True,
                    xalign=0.0
                )
                self.label.add_css_class("message-text")
                self.bubble_box.append(self.label)

        # 3. Meta footer (time, hops, status)
        self.meta_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.meta_box.set_halign(Gtk.Align.END)
        self.meta_box.add_css_class("meta-box")

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

    def _create_image_widget(self) -> Optional[Gtk.Widget]:
        if not self.image_path or not os.path.isfile(self.image_path):
            return None

        try:
            gio_file = Gio.File.new_for_path(self.image_path)
            texture = Gdk.Texture.new_from_file(gio_file)
        except Exception as e:
            GLib.idle_add(lambda: print(f"Error loading texture: {e}"))
            err_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            err_box.add_css_class("image-error-box")
            icon = Gtk.Image.new_from_icon_name("image-missing-symbolic")
            lbl = Gtk.Label(label="Bild konnte nicht geladen werden")
            err_box.append(icon)
            err_box.append(lbl)
            return err_box

        w = texture.get_width()
        h = texture.get_height()

        # Proportional thumbnail scaling within max 300x300
        max_thumb_w = 300
        max_thumb_h = 300
        scale = min(max_thumb_w / max(1, w), max_thumb_h / max(1, h), 1.0)
        tw = max(80, int(w * scale))
        th = max(60, int(h * scale))

        pic = Gtk.Picture.new_for_paintable(texture)
        pic.set_can_shrink(True)
        if hasattr(Gtk, "ContentFit") and hasattr(pic, "set_content_fit"):
            pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        elif hasattr(pic, "set_keep_aspect_ratio"):
            pic.set_keep_aspect_ratio(True)
        pic.set_size_request(tw, th)
        pic.add_css_class("message-image")

        # Container for rounded corners and click gesture
        img_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        img_container.add_css_class("message-image-container")
        img_container.append(pic)

        # Click / Tap gesture
        click_gesture = Gtk.GestureClick.new()
        click_gesture.connect("released", self._on_image_clicked_event)
        img_container.add_controller(click_gesture)
        img_container.set_cursor(Gdk.Cursor.new_from_name("pointer", None))

        return img_container

    def _on_image_clicked_event(self, _gesture, _n_press, _x, _y):
        if self.on_image_clicked and self.image_path:
            self.on_image_clicked(self.image_path, self.message_data)

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
