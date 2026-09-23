"""Chat view widget managing the message history and message composer."""

import os
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, Pango

from retchat.widgets.message_bubble import MessageBubble
from retchat.dialogs.image_viewer_dialog import ImageViewerDialog


class ChatView(Adw.Bin):
    def __init__(
        self,
        on_back_clicked: Callable[[], None],
        on_send_message: Callable[..., None],
        on_rename_contact: Callable[[str], None],
        on_copy_hash: Callable[[str], None],
        on_request_path: Callable[[str], None]
    ):
        super().__init__()
        self.set_vexpand(True)
        self.set_hexpand(True)

        self.toolbar_view = Adw.ToolbarView()
        self.toolbar_view.set_bottom_bar_style(Adw.ToolbarStyle.FLAT)
        self.set_child(self.toolbar_view)

        self.on_back_clicked = on_back_clicked
        self.on_send_message = on_send_message
        self.on_rename_contact = on_rename_contact
        self.on_copy_hash = on_copy_hash
        self.on_request_path = on_request_path

        self.current_dest_hash: Optional[str] = None
        self.current_conv_data: Optional[Dict[str, Any]] = None
        self.bubble_widgets: Dict[str, MessageBubble] = {}
        self.pending_image_path: Optional[str] = None

        # 1. Header Bar (Top Bar)
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")

        # Back / Sidebar Toggle button
        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.back_btn.set_tooltip_text("Zurück zu den Chats")
        self.back_btn.add_css_class("flat")
        self.back_btn.connect("clicked", lambda _b: self.on_back_clicked())
        self.header_bar.pack_start(self.back_btn)

        # Window Title
        self.window_title = Adw.WindowTitle(title="Chat", subtitle="")
        self.header_bar.set_title_widget(self.window_title)

        # Menu Button (Actions)
        self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        self.menu_btn.set_tooltip_text("Optionen")
        self._build_menu()
        self.header_bar.pack_end(self.menu_btn)

        self.toolbar_view.add_top_bar(self.header_bar)

        # 2. Scrolled Messages Container (Middle, vexpand=True)
        self.scrolled_window = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.messages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.messages_box.set_margin_top(12)
        self.messages_box.set_margin_bottom(18)
        self.messages_box.set_margin_start(16)
        self.messages_box.set_margin_end(16)

        clamp = Adw.Clamp(maximum_size=700, child=self.messages_box)
        self.scrolled_window.set_child(clamp)
        self._is_at_bottom = True
        self._force_scroll_to_bottom = True
        vadj = self.scrolled_window.get_vadjustment()
        if vadj:
            vadj.connect("value-changed", self._on_scroll_value_changed)
            vadj.connect("notify::upper", self._on_bounds_changed)
            vadj.connect("notify::page-size", self._on_bounds_changed)
        self.toolbar_view.set_content(self.scrolled_window)

        # 3. Bottom Bar with Message Composer (Bottom, vexpand=False)
        composer_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        composer_container.set_margin_top(6)
        composer_container.set_margin_bottom(6)
        composer_container.set_margin_start(12)
        composer_container.set_margin_end(12)

        # Image attachment preview chip (visible when an image is attached)
        self.preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.preview_box.add_css_class("image-attachment-chip")
        self.preview_box.set_visible(False)

        self.preview_thumb = Gtk.Picture()
        self.preview_thumb.set_size_request(38, 38)
        self.preview_thumb.set_can_shrink(True)
        if hasattr(Gtk, "ContentFit") and hasattr(self.preview_thumb, "set_content_fit"):
            self.preview_thumb.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
        elif hasattr(self.preview_thumb, "set_keep_aspect_ratio"):
            self.preview_thumb.set_keep_aspect_ratio(True)
        self.preview_thumb.add_css_class("image-attachment-thumb")
        self.preview_box.append(self.preview_thumb)

        self.preview_label = Gtk.Label(
            label="",
            ellipsize=Pango.EllipsizeMode.MIDDLE,
            hexpand=True,
            xalign=0.0
        )
        self.preview_box.append(self.preview_label)

        remove_btn = Gtk.Button(icon_name="window-close-symbolic")
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("circular")
        remove_btn.set_tooltip_text("Bild entfernen")
        remove_btn.connect("clicked", lambda _b: self._clear_pending_image())
        self.preview_box.append(remove_btn)

        composer_container.append(self.preview_box)

        # Composer inputs row
        composer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Attach image button
        self.attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic")
        self.attach_btn.add_css_class("flat")
        self.attach_btn.add_css_class("circular")
        self.attach_btn.set_tooltip_text("Bild anhängen")
        self.attach_btn.connect("clicked", self._on_attach_image_clicked)
        composer_box.append(self.attach_btn)

        # Text Entry
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Nachricht über Reticulum verfassen...")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_send_clicked)
        self.entry.connect("notify::has-focus", self._on_entry_focus)
        composer_box.append(self.entry)

        # Send Button
        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("circular")
        self.send_btn.set_tooltip_text("Nachricht senden (Enter)")
        self.send_btn.connect("clicked", self._on_send_clicked)
        composer_box.append(self.send_btn)

        composer_container.append(composer_box)

        clamp_composer = Adw.Clamp(maximum_size=700, child=composer_container)
        self.bottom_bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.bottom_bar.add_css_class("chat-bottom-bar")
        self.bottom_bar.append(clamp_composer)
        self.toolbar_view.add_bottom_bar(self.bottom_bar)

        self.toolbar_view.connect(
            "notify::bottom-bar-height",
            lambda _tv, _pspec: self._sync_bottom_margin()
        )
        self._sync_bottom_margin()

    def _build_menu(self):
        menu_model = Gio.Menu()
        menu_model.append("Sync", "win.sync")
        menu_model.append("Pfad im Mesh anfragen", "win.request_path")
        menu_model.append("Kontakt umbenennen", "win.rename")
        menu_model.append("Ziel-Hash kopieren", "win.copy_hash")
        self.menu_btn.set_menu_model(menu_model)

    def set_back_button_mode(self, is_collapsed: bool):
        if is_collapsed:
            self.back_btn.set_icon_name("go-previous-symbolic")
            self.back_btn.set_tooltip_text("Zurück zu den Chats")
        else:
            self.back_btn.set_icon_name("sidebar-show-symbolic")
            self.back_btn.set_tooltip_text("Seitenleiste ein-/ausblenden")

    def set_back_button_visible(self, visible: bool):
        self.back_btn.set_visible(visible)

    def update_header(self, conv_data: Optional[Dict[str, Any]] = None):
        if conv_data:
            self.current_conv_data = conv_data
        if not self.current_conv_data:
            return

        custom_name = self.current_conv_data.get("custom_name")
        display_name = self.current_conv_data.get("display_name")
        dest_hash = self.current_dest_hash or self.current_conv_data.get("destination_hash", "")

        title = custom_name or display_name or f"[{dest_hash[:8]}...{dest_hash[-4:]}]"
        hops = self.current_conv_data.get("hops")
        if hops is None:
            hops_str = "Pfad unbekannt"
        elif hops == 0:
            hops_str = "Direkt erreichbar"
        else:
            hops_str = f"{hops} Hop{'s' if hops > 1 else ''} entfernt"
        subtitle = f"{hops_str} • {dest_hash[:16]}..."

        self.window_title.set_title(title)
        self.window_title.set_subtitle(subtitle)

    def load_conversation(self, conv_data: Dict[str, Any], messages: List[Dict[str, Any]]):
        self.current_conv_data = conv_data
        self.current_dest_hash = conv_data["destination_hash"].lower()
        self._clear_pending_image()

        # Update header title
        self.update_header(conv_data)

        # Clear existing bubbles
        while child := self.messages_box.get_first_child():
            self.messages_box.remove(child)
        self.bubble_widgets.clear()

        # Add message bubbles
        for msg in messages:
            self._add_bubble_widget(msg)

        # Scroll to bottom after layout
        self._scroll_after_layout()

        # Focus text entry
        self.entry.grab_focus()

    def _add_bubble_widget(self, msg_data: Dict[str, Any]):
        bubble = MessageBubble(msg_data, on_image_clicked=self._on_message_image_clicked)
        self.bubble_widgets[msg_data["message_hash"]] = bubble
        self.messages_box.append(bubble)

    def _sync_bottom_margin(self):
        composer_h = self.toolbar_view.get_bottom_bar_height()
        self.messages_box.set_margin_bottom(18 + composer_h)

    def append_message(self, msg_data: Dict[str, Any]):
        if msg_data.get("conversation_hash", "").lower() != (self.current_dest_hash or "").lower():
            return

        # Check if already present
        msg_hash = msg_data.get("message_hash")
        if msg_hash in self.bubble_widgets:
            self.bubble_widgets[msg_hash].update_state(msg_data.get("state", 0))
            return

        self._add_bubble_widget(msg_data)
        if self._is_at_bottom or msg_data.get("is_outgoing", False):
            self._scroll_after_layout()

    def update_message_state(self, message_hash: str, state: int):
        if message_hash in self.bubble_widgets:
            self.bubble_widgets[message_hash].update_state(state)

    def _scroll_to_bottom(self):
        self._is_at_bottom = True
        adj = self.scrolled_window.get_vadjustment()
        if adj:
            max_v = max(0.0, adj.get_upper() - adj.get_page_size())
            adj.set_value(max_v)
        return False

    def _scroll_after_layout(self):
        self._is_at_bottom = True
        self._force_scroll_to_bottom = True
        GLib.idle_add(self._scroll_to_bottom, priority=GLib.PRIORITY_LOW)

    def _on_scroll_value_changed(self, adj):
        if getattr(self, "_force_scroll_to_bottom", False):
            return
        max_val = adj.get_upper() - adj.get_page_size()
        if max_val <= 0:
            self._is_at_bottom = True
        else:
            diff = max_val - adj.get_value()
            self._is_at_bottom = (diff <= 100.0)

    def _on_bounds_changed(self, adj, _pspec):
        if getattr(self, "_is_at_bottom", True) or getattr(self, "_force_scroll_to_bottom", False):
            self._scroll_to_bottom()
            self._force_scroll_to_bottom = False

    def _on_entry_focus(self, entry, _pspec):
        if entry.has_focus():
            self._scroll_after_layout()

    def _set_pending_image(self, file_path: str):
        if not file_path or not os.path.isfile(file_path):
            return
        self.pending_image_path = file_path
        fname = os.path.basename(file_path)
        try:
            size_kb = os.path.getsize(file_path) / 1024
            self.preview_label.set_text(f"{fname} ({size_kb:.1f} KB)")
        except Exception:
            self.preview_label.set_text(fname)

        try:
            gio_f = Gio.File.new_for_path(file_path)
            tex = Gdk.Texture.new_from_file(gio_f)
            self.preview_thumb.set_paintable(tex)
        except Exception:
            pass

        self.preview_box.set_visible(True)
        self.entry.grab_focus()
        GLib.idle_add(self._sync_bottom_margin)

    def _clear_pending_image(self):
        self.pending_image_path = None
        self.preview_box.set_visible(False)
        GLib.idle_add(self._sync_bottom_margin)

    def _on_attach_image_clicked(self, _btn):
        root = self.get_root()
        parent_win = root if isinstance(root, Gtk.Window) else None

        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog()
            dialog.set_title("Bild auswählen")

            filter_img = Gtk.FileFilter()
            filter_img.set_name("Bilder")
            filter_img.add_mime_type("image/jpeg")
            filter_img.add_mime_type("image/png")
            filter_img.add_mime_type("image/webp")
            filter_img.add_mime_type("image/gif")
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filter_img)
            dialog.set_filters(filters)
            dialog.set_default_filter(filter_img)

            def on_open_finish(d, result):
                try:
                    f = d.open_finish(result)
                    if f and f.get_path():
                        self._set_pending_image(f.get_path())
                except Exception:
                    pass

            dialog.open(parent_win, None, on_open_finish)
        else:
            fc = Gtk.FileChooserNative.new(
                "Bild auswählen",
                parent_win,
                Gtk.FileChooserAction.OPEN,
                "Auswählen",
                "Abbrechen"
            )
            filter_img = Gtk.FileFilter()
            filter_img.set_name("Bilder")
            filter_img.add_mime_type("image/*")
            fc.add_filter(filter_img)

            def on_resp(d, resp):
                if resp == Gtk.ResponseType.ACCEPT:
                    f = d.get_file()
                    if f and f.get_path():
                        self._set_pending_image(f.get_path())
                d.destroy()

            fc.connect("response", on_resp)
            fc.show()

    def _on_message_image_clicked(self, image_path: str, msg_data: Dict[str, Any]):
        root = self.get_root()
        parent_win = root if isinstance(root, Gtk.Window) else None
        dialog = ImageViewerDialog(
            parent_window=parent_win,
            image_path=image_path,
            image_name=msg_data.get("image_name"),
            image_size=msg_data.get("image_size")
        )
        dialog.present()

    def _on_send_clicked(self, _widget):
        text = self.entry.get_text().strip()
        img_p = self.pending_image_path
        if not text and not img_p:
            return
        if not self.current_dest_hash:
            return
        self.entry.set_text("")
        self._clear_pending_image()
        self._scroll_after_layout()
        self.on_send_message(self.current_dest_hash, text, img_p)
