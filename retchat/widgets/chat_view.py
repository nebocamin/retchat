"""Chat view for direct LXMF conversations: message history and composer."""

import os
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from retchat.dialogs.image_viewer_dialog import ImageViewerDialog
from retchat.models import MessageItem
from retchat.reticulum_service import MAX_ATTACHMENT_SIZE, PROPAGATION_SIZE_LIMIT, is_sendable_image
from retchat.widgets.attachment_row import file_icon, is_executable, named_copy
from retchat.widgets.chat_history import CONTENT_MAX_WIDTH, ChatHistory
from retchat.widgets.message_bubble import MessageBubble, load_thumbnail


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
        self.pending_attachment_path: Optional[str] = None

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

        # Actions of the attachment rows in the bubbles (target: path, name)
        actions = Gio.SimpleActionGroup()
        for name, handler in (("open-attachment", self._open_attachment),
                              ("save-attachment", self._save_attachment)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("(ss)"))
            action.connect("activate", lambda _a, p, fn=handler: fn(*p.unpack()))
            actions.add_action(action)
        self.insert_action_group("chat", actions)

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

        # Attachment preview chip: image thumbnail or file type icon, name and size
        self.preview_box = Gtk.Box(spacing=8, visible=False)
        self.preview_box.add_css_class("attachment-chip")
        self.preview_thumb = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.preview_thumb.set_size_request(36, 36)
        self.preview_thumb.set_overflow(Gtk.Overflow.HIDDEN)
        self.preview_thumb.add_css_class("attachment-thumb")
        self.preview_box.append(self.preview_thumb)
        self.preview_icon = Gtk.Image(pixel_size=32, valign=Gtk.Align.CENTER)
        self.preview_box.append(self.preview_icon)
        preview_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.preview_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.MIDDLE, xalign=0.0)
        preview_text.append(self.preview_label)
        self.preview_hint = Gtk.Label(xalign=0.0, wrap=True,
                                      label="Nur direkt zustellbar, zu groß für Propagation-Nodes")
        self.preview_hint.add_css_class("attachment-hint")
        preview_text.append(self.preview_hint)
        self.preview_box.append(preview_text)
        remove_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Anhang entfernen",
                                valign=Gtk.Align.CENTER)
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("circular")
        remove_btn.connect("clicked", lambda _b: self._clear_pending_attachment())
        self.preview_box.append(remove_btn)
        box.append(self.preview_box)

        # Input row
        row = Gtk.Box(spacing=6)
        attach_btn = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text="Datei anhängen",
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
        self._clear_pending_attachment()
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
        self._clear_pending_attachment()
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

    def replace_message_hash(self, old_hash: str, new_hash: str):
        """A queued message was sent and got its LXMF hash; follow it from now on."""
        item = self._items.pop(old_hash, None)
        if item is not None:
            item.message_hash = new_hash
            self._items[new_hash] = item

    def _update_empty_state(self):
        empty = self.history.store.get_n_items() == 0
        self.history_stack.set_visible_child_name("empty" if empty else "messages")

    # --- Composer -------------------------------------------------------------

    def _update_send_sensitivity(self):
        has_content = bool(self.entry.get_text().strip()) or self.pending_attachment_path is not None
        self.send_btn.set_sensitive(has_content)

    def _send(self):
        text = self.entry.get_text().strip()
        attachment = self.pending_attachment_path
        if not self.current_dest_hash or (not text and not attachment):
            return
        self.entry.set_text("")
        self._clear_pending_attachment()
        # Jump to the end and stick there, so the own message is followed.
        self.history.scroll_to_bottom()
        self._on_send_message(self.current_dest_hash, text, attachment)

    def _set_pending_attachment(self, file_path: str):
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return
        name = os.path.basename(file_path)
        image = is_sendable_image(file_path)
        # Images are downscaled before sending, so only files are limited here.
        if not image and size > MAX_ATTACHMENT_SIZE:
            self._toast(f"«{name}» ist zu groß ({GLib.format_size(size)}, "
                        f"höchstens {GLib.format_size(MAX_ATTACHMENT_SIZE)})")
            return

        self.pending_attachment_path = file_path
        self.preview_label.set_text(f"{name} ({GLib.format_size(size)})")
        texture = load_thumbnail(file_path, os.path.getmtime(file_path)) if image else None
        self.preview_thumb.set_paintable(texture)
        self.preview_thumb.set_visible(texture is not None)
        self.preview_icon.set_from_gicon(file_icon(name, file_path))
        self.preview_icon.set_visible(texture is None)
        self.preview_hint.set_visible(not image and size > PROPAGATION_SIZE_LIMIT)
        self.preview_box.set_visible(True)
        self._update_send_sensitivity()
        self.entry.grab_focus()

    def _clear_pending_attachment(self):
        self.pending_attachment_path = None
        self.preview_thumb.set_paintable(None)
        self.preview_box.set_visible(False)
        self._update_send_sensitivity()

    def _on_attach_clicked(self, _btn):
        all_files = Gtk.FileFilter(name="Alle Dateien")
        all_files.add_pattern("*")
        images = Gtk.FileFilter(name="Bilder")
        images.add_mime_type("image/*")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(all_files)
        filters.append(images)

        dialog = Gtk.FileDialog(title="Datei anhängen", filters=filters, default_filter=all_files)
        dialog.open(self.get_root(), None, self._on_attach_finished)

    def _on_attach_finished(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # dismissed
        if file and file.get_path():
            self._set_pending_attachment(file.get_path())

    # --- Attachments in bubbles --------------------------------------------------

    def _toast(self, text: str):
        overlay = self.get_ancestor(Adw.ToastOverlay)
        if overlay is not None:
            overlay.add_toast(Adw.Toast(title=text, timeout=3))

    def _open_attachment(self, path: str, name: str):
        if is_executable(name, path):  # the button is hidden; also refuse here
            self._toast(f"«{name}» ist ausführbar und wird nicht geöffnet. Speichere die Datei, um sie zu prüfen.")
            return
        try:
            copy = named_copy(path, name)
        except OSError as e:
            self._toast(f"Datei kann nicht geöffnet werden: {e.strerror or e}")
            return
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(copy))
        launcher.launch(self.get_root(), None, self._on_launch_finished)

    def _on_launch_finished(self, launcher: Gtk.FileLauncher, result: Gio.AsyncResult):
        try:
            launcher.launch_finish(result)
        except GLib.Error as e:
            if not e.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                self._toast(f"Keine Anwendung zum Öffnen gefunden ({e.message})")

    def _save_attachment(self, path: str, name: str):
        dialog = Gtk.FileDialog(title="Datei speichern", initial_name=name)
        dialog.save(self.get_root(), None, self._on_save_finished, path)

    def _on_save_finished(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, path: str):
        try:
            target = dialog.save_finish(result)
        except GLib.Error:
            return  # dismissed
        self.save_attachment_to(path, target)

    def save_attachment_to(self, path: str, target: Gio.File):
        try:
            Gio.File.new_for_path(path).copy(target, Gio.FileCopyFlags.OVERWRITE, None, None)
        except GLib.Error as e:
            self._toast(f"Speichern fehlgeschlagen: {e.message}")
            return
        self._toast(f"Gespeichert: {target.get_basename()}")

    def _on_image_clicked(self, item: MessageItem):
        ImageViewerDialog(
            parent_window=self.get_root(),
            image_path=item.image_path,
            image_name=item.image_name,
            image_size=item.image_size_or_none,
        ).present()
