"""Chat view widget managing the message history and message composer."""

from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk

from retchat.widgets.message_bubble import MessageBubble


class ChatView(Gtk.Box):
    def __init__(
        self,
        on_back_clicked: Callable[[], None],
        on_send_message: Callable[[str, str], None],
        on_rename_contact: Callable[[str], None],
        on_copy_hash: Callable[[str], None],
        on_request_path: Callable[[str], None]
    ):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self.on_back_clicked = on_back_clicked
        self.on_send_message = on_send_message
        self.on_rename_contact = on_rename_contact
        self.on_copy_hash = on_copy_hash
        self.on_request_path = on_request_path

        self.current_dest_hash: Optional[str] = None
        self.current_conv_data: Optional[Dict[str, Any]] = None
        self.bubble_widgets: Dict[str, MessageBubble] = {}

        # 1. Header Bar (Adw.HeaderBar automatically provides back button in collapsed mode)
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")

        # Window Title
        self.window_title = Adw.WindowTitle(title="Chat", subtitle="")
        self.header_bar.set_title_widget(self.window_title)

        # Menu Button (Actions)
        self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        self.menu_btn.set_tooltip_text("Optionen")
        self._build_menu()
        self.header_bar.pack_end(self.menu_btn)

        self.append(self.header_bar)

        # 2. Scrolled Messages Container
        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_vexpand(True)
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.messages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.messages_box.set_margin_top(12)
        self.messages_box.set_margin_bottom(12)
        self.messages_box.set_margin_start(16)
        self.messages_box.set_margin_end(16)
        self.messages_box.set_vexpand(True)
        self.scrolled_window.set_child(self.messages_box)

        self.append(self.scrolled_window)

        # 3. Composer Box
        self.composer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.composer_box.add_css_class("composer-bar")
        self.composer_box.set_margin_top(8)
        self.composer_box.set_margin_bottom(12)
        self.composer_box.set_margin_start(10)
        self.composer_box.set_margin_end(10)

        # Text Entry
        self.entry = Gtk.Entry()
        self.entry.set_width_chars(1)
        self.entry.set_placeholder_text("Nachricht über Reticulum verfassen...")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_send_clicked)
        self.composer_box.append(self.entry)

        # Send Button
        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("circular")
        self.send_btn.set_tooltip_text("Nachricht senden (Enter)")
        self.send_btn.connect("clicked", self._on_send_clicked)
        self.composer_box.append(self.send_btn)

        self.append(self.composer_box)

    def _build_menu(self):
        from gi.repository import Gio
        menu_model = Gio.Menu()
        menu_model.append("Sync", "win.sync")
        menu_model.append("Pfad im Mesh anfragen", "win.request_path")
        menu_model.append("Kontakt umbenennen", "win.rename")
        menu_model.append("Ziel-Hash kopieren", "win.copy_hash")
        self.menu_btn.set_menu_model(menu_model)

    def set_back_button_visible(self, visible: bool):
        self.header_bar.set_show_back_button(visible)

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

        # Update header title
        self.update_header(conv_data)

        # Clear existing bubbles
        while child := self.messages_box.get_first_child():
            self.messages_box.remove(child)
        self.bubble_widgets.clear()

        # Add message bubbles
        for msg in messages:
            self._add_bubble_widget(msg)

        # Scroll to bottom
        self.scroll_to_bottom()

        # Focus text entry
        self.entry.grab_focus()

    def _add_bubble_widget(self, msg_data: Dict[str, Any]):
        bubble = MessageBubble(msg_data)
        self.bubble_widgets[msg_data["message_hash"]] = bubble
        self.messages_box.append(bubble)

    def append_message(self, msg_data: Dict[str, Any]):
        if msg_data.get("conversation_hash", "").lower() != (self.current_dest_hash or ""):
            return

        # Check if already present
        msg_hash = msg_data.get("message_hash")
        if msg_hash in self.bubble_widgets:
            self.bubble_widgets[msg_hash].update_state(msg_data.get("state", 0))
            return

        self._add_bubble_widget(msg_data)
        self.scroll_to_bottom()

    def update_message_state(self, message_hash: str, state: int):
        if message_hash in self.bubble_widgets:
            self.bubble_widgets[message_hash].update_state(state)

    def scroll_to_bottom(self):
        def _do_scroll():
            adj = self.scrolled_window.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        GLib.idle_add(_do_scroll)

    def _on_send_clicked(self, _widget):
        text = self.entry.get_text().strip()
        if not text or not self.current_dest_hash:
            return
        self.entry.set_text("")
        self.on_send_message(self.current_dest_hash, text)
