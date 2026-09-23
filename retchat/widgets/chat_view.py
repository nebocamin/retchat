"""Chat view for direct LXMF conversations: message history and composer."""

import os
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from retchat.dialogs.image_viewer_dialog import ImageViewerDialog
from retchat.models import MessageItem
from retchat.widgets.chat_history import CONTENT_MAX_WIDTH, ChatHistory
from retchat.widgets.message_bubble import MessageBubble


class ChatView(Adw.Bin):
    """Shows one LXMF conversation (see ChatHistory for the scrolling behaviour)."""

    def __init__(
        self,
        on_back_clicked: Callable[[], None],
        on_send_message: Callable[[str, str, Optional[str]], None],
    ):
        super().__init__(hexpand=True, vexpand=True)
        self._on_back_clicked = on_back_clicked
        self._on_send_message = on_send_message

        self.current_dest_hash: Optional[str] = None
        self.current_conv_data: Optional[Dict[str, Any]] = None
        self.pending_image_path: Optional[str] = None

        self._items: Dict[str, MessageItem] = {}
        self.history = ChatHistory(
            MessageItem, lambda: MessageBubble(on_image_clicked=self._on_image_clicked)
        )

        toolbar_view = Adw.ToolbarView(bottom_bar_style=Adw.ToolbarStyle.RAISED)
        toolbar_view.add_top_bar(self._build_header())
        toolbar_view.set_content(self._build_content())
        toolbar_view.add_bottom_bar(self._build_composer())
        self.set_child(toolbar_view)

        self.history.store.connect("items-changed", lambda *_: self._update_empty_state())
        self._update_empty_state()

    # --- UI construction ------------------------------------------------------

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar()

        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Zurück zu den Chats")
        self.back_btn.connect("clicked", lambda _b: self._on_back_clicked())
        header.pack_start(self.back_btn)

        self.window_title = Adw.WindowTitle(title="Chat")
        header.set_title_widget(self.window_title)

        self.menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="Optionen")
        self._update_menu()
        header.pack_end(self.menu_btn)
        return header

    def _build_content(self) -> Gtk.Widget:
        self.empty_page = Adw.StatusPage(
            icon_name="mail-send-symbolic",
            title="Noch keine Nachrichten",
            description="Schreib die erste Nachricht – sie wird Ende-zu-Ende-verschlüsselt über Reticulum zugestellt.",
        )
        self.empty_page.add_css_class("compact")

        self.history_stack = Gtk.Stack()
        self.history_stack.add_named(self.history, "messages")
        self.history_stack.add_named(self.empty_page, "empty")
        return self.history_stack

    def _build_composer(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("composer")

        # Attachment preview chip
        self.preview_box = Gtk.Box(spacing=8, visible=False)
        self.preview_box.add_css_class("attachment-chip")
        self.preview_thumb = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.preview_thumb.set_size_request(36, 36)
        self.preview_thumb.set_overflow(Gtk.Overflow.HIDDEN)
        self.preview_thumb.add_css_class("attachment-thumb")
        self.preview_box.append(self.preview_thumb)
        self.preview_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True, xalign=0.0)
        self.preview_box.append(self.preview_label)
        remove_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Bild entfernen",
                                valign=Gtk.Align.CENTER)
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("circular")
        remove_btn.connect("clicked", lambda _b: self._clear_pending_image())
        self.preview_box.append(remove_btn)
        box.append(self.preview_box)

        # Input row
        row = Gtk.Box(spacing=6)
        attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Bild anhängen",
                                valign=Gtk.Align.CENTER)
        attach_btn.add_css_class("flat")
        attach_btn.add_css_class("circular")
        attach_btn.connect("clicked", self._on_attach_clicked)
        row.append(attach_btn)

        self.entry = Gtk.Entry(placeholder_text="Nachricht", hexpand=True, valign=Gtk.Align.CENTER)
        self.entry.connect("activate", lambda _e: self._send())
        self.entry.connect("changed", lambda _e: self._update_send_sensitivity())
        row.append(self.entry)

        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic", tooltip_text="Senden (Enter)",
                                   valign=Gtk.Align.CENTER, sensitive=False)
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("circular")
        self.send_btn.connect("clicked", lambda _b: self._send())
        row.append(self.send_btn)
        box.append(row)

        return Adw.Clamp(child=box, maximum_size=CONTENT_MAX_WIDTH,
                         tightening_threshold=CONTENT_MAX_WIDTH - 120)

    def _update_menu(self):
        """Chat menu; the delete entry targets the currently shown contact."""
        menu = Gio.Menu()
        contact = Gio.Menu()
        contact.append("Pfad im Mesh anfragen", "win.request_path")
        contact.append("Kontakt umbenennen", "win.rename")
        contact.append("Ziel-Hash kopieren", "win.copy_hash")
        menu.append_section(None, contact)
        if self.current_dest_hash:
            danger = Gio.Menu()
            item = Gio.MenuItem.new("Chat löschen…", None)
            item.set_action_and_target_value(
                "win.delete_conversation", GLib.Variant.new_string(self.current_dest_hash)
            )
            danger.append_item(item)
            menu.append_section(None, danger)
        self.menu_btn.set_menu_model(menu)

    # --- Public API -----------------------------------------------------------

    def clear(self):
        """Forget the shown conversation (e.g. after it was deleted)."""
        self.current_dest_hash = None
        self.current_conv_data = None
        self._items = {}
        self._clear_pending_image()
        self.entry.set_text("")
        self.history.replace([])
        self.window_title.set_title("Chat")
        self.window_title.set_subtitle("")
        self._update_menu()

    def set_back_button_mode(self, is_collapsed: bool):
        if is_collapsed:
            self.back_btn.set_icon_name("go-previous-symbolic")
            self.back_btn.set_tooltip_text("Zurück zu den Chats")
        else:
            self.back_btn.set_icon_name("sidebar-show-symbolic")
            self.back_btn.set_tooltip_text("Seitenleiste ein-/ausblenden")

    def update_header(self, conv_data: Optional[Dict[str, Any]] = None):
        if conv_data:
            self.current_conv_data = conv_data
        conv = self.current_conv_data
        if not conv:
            return

        dest_hash = self.current_dest_hash or conv.get("destination_hash", "")
        title = conv.get("custom_name") or conv.get("display_name") or f"{dest_hash[:8]}…{dest_hash[-4:]}"

        hops = conv.get("hops")
        if hops is None:
            reach = "Pfad unbekannt"
        elif hops == 0:
            reach = "Direkt erreichbar"
        else:
            reach = f"{hops} Hop{'s' if hops > 1 else ''} entfernt"

        self.window_title.set_title(title)
        self.window_title.set_subtitle(f"{reach} · {dest_hash[:16]}…")

    def load_conversation(self, conv_data: Dict[str, Any], messages: List[Dict[str, Any]]):
        """Replace the shown conversation and jump to the newest message."""
        self.current_conv_data = conv_data
        self.current_dest_hash = conv_data["destination_hash"].lower()
        self._clear_pending_image()
        self.update_header(conv_data)
        self._update_menu()

        items = [MessageItem(m) for m in messages]
        self._items = {item.message_hash: item for item in items}
        self.history.replace(items)
        self.entry.grab_focus()

    def append_message(self, msg_data: Dict[str, Any]):
        if (msg_data.get("conversation_hash") or "").lower() != (self.current_dest_hash or ""):
            return

        existing = self._items.get(msg_data.get("message_hash"))
        if existing is not None:
            existing.state = int(msg_data.get("state") or 0)
            return

        item = MessageItem(msg_data)
        self._items[item.message_hash] = item
        self.history.append(item)

    def update_message_state(self, message_hash: str, state: int):
        item = self._items.get(message_hash)
        if item is not None:
            item.state = state

    def _update_empty_state(self):
        empty = self.history.store.get_n_items() == 0
        self.history_stack.set_visible_child_name("empty" if empty else "messages")

    # --- Composer -------------------------------------------------------------

    def _update_send_sensitivity(self):
        has_content = bool(self.entry.get_text().strip()) or self.pending_image_path is not None
        self.send_btn.set_sensitive(has_content)

    def _send(self):
        text = self.entry.get_text().strip()
        image_path = self.pending_image_path
        if not self.current_dest_hash or (not text and not image_path):
            return
        self.entry.set_text("")
        self._clear_pending_image()
        # Jump to the end and stick there, so the own message is followed.
        self.history.scroll_to_bottom()
        self._on_send_message(self.current_dest_hash, text, image_path)

    def _set_pending_image(self, file_path: str):
        if not os.path.isfile(file_path):
            return
        self.pending_image_path = file_path
        name = os.path.basename(file_path)
        self.preview_label.set_text(f"{name} ({os.path.getsize(file_path) / 1024:.1f} KB)")
        try:
            self.preview_thumb.set_paintable(Gdk.Texture.new_from_file(Gio.File.new_for_path(file_path)))
        except GLib.Error:
            self.preview_thumb.set_paintable(None)
        self.preview_box.set_visible(True)
        self._update_send_sensitivity()
        self.entry.grab_focus()

    def _clear_pending_image(self):
        self.pending_image_path = None
        self.preview_thumb.set_paintable(None)
        self.preview_box.set_visible(False)
        self._update_send_sensitivity()

    def _on_attach_clicked(self, _btn):
        image_filter = Gtk.FileFilter(name="Bilder")
        for mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            image_filter.add_mime_type(mime)
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(image_filter)

        dialog = Gtk.FileDialog(title="Bild auswählen", filters=filters, default_filter=image_filter)
        dialog.open(self.get_root(), None, self._on_attach_finished)

    def _on_attach_finished(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # dismissed
        if file and file.get_path():
            self._set_pending_image(file.get_path())

    def _on_image_clicked(self, item: MessageItem):
        ImageViewerDialog(
            parent_window=self.get_root(),
            image_path=item.image_path,
            image_name=item.image_name,
            image_size=item.image_size_or_none,
        ).present()
