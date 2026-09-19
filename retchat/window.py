"""Main Application Window for Retchat."""

from typing import Any, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk

import RNS
from retchat.database import Database
from retchat.dialogs.interfaces_dialog import InterfacesDialog
from retchat.dialogs.new_chat_dialog import NewChatDialog
from retchat.dialogs.profile_dialog import ProfileDialog
from retchat.reticulum_service import ReticulumService
from retchat.widgets.announce_row import AnnounceRow
from retchat.widgets.chat_view import ChatView
from retchat.widgets.conversation_row import ConversationRow


class RetchatWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, db: Database, service: ReticulumService):
        super().__init__(application=app)

        self.db = db
        self.service = service

        self.set_title("Retchat")
        self.set_default_size(960, 680)

        self.current_dest_hash: Optional[str] = None
        self.conv_rows: Dict[str, ConversationRow] = {}
        self.announce_rows: Dict[str, AnnounceRow] = {}

        # Toast Overlay
        self.toast_overlay = Adw.ToastOverlay()

        # Split View (Sidebar + Content)
        self.split_view = Adw.NavigationSplitView()
        self.split_view.set_min_sidebar_width(300)
        self.split_view.set_max_sidebar_width(380)

        # Build Sidebar and Content
        sidebar_widget = self._build_sidebar()
        content_widget = self._build_content()

        sidebar_page = Adw.NavigationPage.new(sidebar_widget, "Unterhaltungen")
        content_page = Adw.NavigationPage.new(content_widget, "Chat")

        self.split_view.set_sidebar(sidebar_page)
        self.split_view.set_content(content_page)

        self.toast_overlay.set_child(self.split_view)
        self.set_content(self.toast_overlay)

        # Bind collapsed state to ChatView back button
        self.split_view.bind_property(
            "collapsed",
            self.chat_view.back_btn,
            "visible",
            GObject.BindingFlags.SYNC_CREATE
        )

        # Setup GActions for window
        self._setup_actions()

        # Connect Service Callbacks
        self.service.add_message_received_callback(self._on_service_message_received)
        self.service.add_message_state_callback(self._on_service_message_state)
        self.service.add_announce_callback(self._on_service_announce_received)

        # Initial data loading
        self._ensure_default_contact()
        self._load_conversations()
        self._load_announces()

    def _setup_actions(self):
        # Action: Copy Hash
        action_copy = Gio.SimpleAction.new("chat.copy_hash", None)
        action_copy.connect("activate", lambda _a, _p: self._action_copy_hash())
        self.add_action(action_copy)

        # Action: Rename Contact
        action_rename = Gio.SimpleAction.new("chat.rename", None)
        action_rename.connect("activate", lambda _a, _p: self._action_rename_contact())
        self.add_action(action_rename)

        # Action: Request Path
        action_path = Gio.SimpleAction.new("chat.request_path", None)
        action_path.connect("activate", lambda _a, _p: self._action_request_path())
        self.add_action(action_path)

    # --- UI Builders ---
    def _build_sidebar(self) -> Gtk.Widget:
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Header Bar
        header = Adw.HeaderBar()
        header.add_css_class("flat")

        # Profile button
        self.profile_btn = Gtk.Button(icon_name="avatar-default-symbolic")
        self.profile_btn.set_tooltip_text("Eigene Identität & Profil")
        self.profile_btn.connect("clicked", lambda _b: self._show_profile_dialog())
        header.pack_start(self.profile_btn)

        # Title
        title_widget = Adw.WindowTitle(title="Retchat", subtitle="Reticulum Mesh")
        header.set_title_widget(title_widget)

        # Interfaces status button
        iface_btn = Gtk.Button(icon_name="network-wireless-symbolic")
        iface_btn.set_tooltip_text("Reticulum Schnittstellen & Status")
        iface_btn.connect("clicked", lambda _b: self._show_interfaces_dialog())
        header.pack_end(iface_btn)

        # New chat button (+)
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.add_css_class("suggested-action")
        add_btn.set_tooltip_text("Neuen Chat starten")
        add_btn.connect("clicked", lambda _b: self._show_new_chat_dialog())
        header.pack_end(add_btn)

        sidebar_box.append(header)

        # Search Entry
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Chats durchsuchen...")
        self.search_entry.set_margin_start(10)
        self.search_entry.set_margin_end(10)
        self.search_entry.set_margin_top(6)
        self.search_entry.set_margin_bottom(6)
        self.search_entry.connect("search-changed", self._on_search_changed)
        sidebar_box.append(self.search_entry)

        # View Switcher (Chats vs Entdecken)
        stack_switcher = Gtk.StackSwitcher()
        stack_switcher.set_halign(Gtk.Align.CENTER)
        stack_switcher.set_margin_top(4)
        stack_switcher.set_margin_bottom(6)

        sidebar_stack = Gtk.Stack()
        sidebar_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack_switcher.set_stack(sidebar_stack)
        sidebar_box.append(stack_switcher)

        # Tab 1: Chats
        conv_scroll = Gtk.ScrolledWindow()
        conv_scroll.set_vexpand(True)
        conv_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.conv_list_box = Gtk.ListBox()
        self.conv_list_box.add_css_class("navigation-sidebar")
        self.conv_list_box.connect("row-selected", self._on_conv_selected)

        # Empty state for chats
        self.conv_empty_page = Adw.StatusPage()
        self.conv_empty_page.set_icon_name("mail-message-new-symbolic")
        self.conv_empty_page.set_title("Keine Chats")
        self.conv_empty_page.set_description("Starte einen Chat über das '+' Symbol oben.")
        self.conv_list_box.set_placeholder(self.conv_empty_page)

        conv_scroll.set_child(self.conv_list_box)
        sidebar_stack.add_titled(conv_scroll, "chats", "Chats")

        # Tab 2: Entdecken (Mesh Peers)
        announce_scroll = Gtk.ScrolledWindow()
        announce_scroll.set_vexpand(True)
        announce_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.announce_list_box = Gtk.ListBox()
        self.announce_list_box.add_css_class("navigation-sidebar")

        # Empty state for announces
        self.announce_empty_page = Adw.StatusPage()
        self.announce_empty_page.set_icon_name("network-transmit-receive-symbolic")
        self.announce_empty_page.set_title("Warte auf Mesh-Peers...")
        self.announce_empty_page.set_description(
            "Sobald Teilnehmer im Reticulum-Netzwerk ein Announce senden, erscheinen sie hier."
        )
        self.announce_list_box.set_placeholder(self.announce_empty_page)

        announce_scroll.set_child(self.announce_list_box)
        sidebar_stack.add_titled(announce_scroll, "discover", "Entdecken")

        sidebar_box.append(sidebar_stack)
        return sidebar_box

    def _build_content(self) -> Gtk.Widget:
        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        # 1. Empty placeholder page
        empty_page = Adw.StatusPage()
        empty_page.set_icon_name("network-wireless-symbolic")
        empty_page.set_title("Retchat")
        empty_page.set_description(
            "Wähle eine Unterhaltung aus oder starte einen neuen Chat über das Reticulum-Mesh-Netzwerk."
        )

        empty_btn = Gtk.Button(label="Neuen Chat starten")
        empty_btn.add_css_class("suggested-action")
        empty_btn.add_css_class("pill")
        empty_btn.set_halign(Gtk.Align.CENTER)
        empty_btn.connect("clicked", lambda _b: self._show_new_chat_dialog())
        empty_page.set_child(empty_btn)

        self.content_stack.add_named(empty_page, "empty")

        # 2. Chat View
        self.chat_view = ChatView(
            on_back_clicked=self._on_chat_back_clicked,
            on_send_message=self._on_send_message,
            on_rename_contact=self._action_rename_contact,
            on_copy_hash=self._action_copy_hash,
            on_request_path=self._action_request_path
        )
        self.content_stack.add_named(self.chat_view, "chat")

        self.content_stack.set_visible_child_name("empty")
        return self.content_stack

    # --- Pre-populating Contact ---
    def _ensure_default_contact(self):
        """Pre-populate the user's specified contact if not present."""
        test_contact_hash = "8d883cfe6c1a846d8f34e5a95a149fdb"
        existing = self.db.get_conversation(test_contact_hash)
        if not existing:
            self.db.add_or_update_conversation(
                dest_hash=test_contact_hash,
                custom_name="Test-Kontakt",
                display_name="Reticulum Gegenstelle",
                hops=1
            )

    # --- Conversation List Management ---
    def _load_conversations(self, query: Optional[str] = None):
        conversations = self.db.get_conversations(query=query)

        # Clear existing rows
        while child := self.conv_list_box.get_first_child():
            self.conv_list_box.remove(child)
        self.conv_rows.clear()

        for conv in conversations:
            row = ConversationRow(conv)
            self.conv_rows[conv["destination_hash"]] = row
            self.conv_list_box.append(row)

    def _on_search_changed(self, entry: Gtk.SearchEntry):
        q = entry.get_text().strip()
        self._load_conversations(query=q if q else None)

    def _on_conv_selected(self, _list_box, row: Optional[ConversationRow]):
        if row is None:
            return
        dest_hash = row.dest_hash
        self.open_conversation(dest_hash)

    def open_conversation(self, dest_hash: str):
        dest_hash = dest_hash.lower()
        conv = self.db.get_conversation(dest_hash)
        if not conv:
            conv = self.db.add_or_update_conversation(dest_hash=dest_hash)

        self.current_dest_hash = dest_hash
        self.db.clear_unread_count(dest_hash)

        # Update row badge
        if dest_hash in self.conv_rows:
            conv["unread_count"] = 0
            self.conv_rows[dest_hash].update_data(conv)

        # Load messages
        messages = self.db.get_messages(dest_hash)
        self.chat_view.load_conversation(conv, messages)
        self.content_stack.set_visible_child_name("chat")

        # Show content in collapsed mode
        self.split_view.set_show_content(True)

    def _on_chat_back_clicked(self):
        self.split_view.set_show_content(False)

    # --- Announce Discovery Management ---
    def _load_announces(self):
        announces = self.db.get_announces()
        while child := self.announce_list_box.get_first_child():
            self.announce_list_box.remove(child)
        self.announce_rows.clear()

        for ann in announces:
            row = AnnounceRow(ann, on_start_chat=self._on_announce_start_chat)
            self.announce_rows[ann["destination_hash"]] = row
            self.announce_list_box.append(row)

    def _on_announce_start_chat(self, dest_hash: str, display_name: str):
        # Create or update conversation and open it
        self.db.add_or_update_conversation(dest_hash=dest_hash, display_name=display_name)
        self._load_conversations()
        self.open_conversation(dest_hash)

    # --- Sending Messages ---
    def _on_send_message(self, dest_hash: str, content: str):
        try:
            msg_data = self.service.send_message(dest_hash, content)
            self.chat_view.append_message(msg_data)

            # Update conversation list
            self._update_or_add_conv_row(dest_hash)
        except Exception as e:
            self._show_toast(f"Fehler beim Senden: {e}")

    def _update_or_add_conv_row(self, dest_hash: str):
        conv = self.db.get_conversation(dest_hash)
        if not conv:
            return

        if dest_hash in self.conv_rows:
            self.conv_rows[dest_hash].update_data(conv)
        else:
            row = ConversationRow(conv)
            self.conv_rows[dest_hash] = row
            self.conv_list_box.prepend(row)

    # --- Service Callbacks (Runs on GLib Main Loop) ---
    def _on_service_message_received(self, msg_data: Dict[str, Any]):
        sender = msg_data.get("sender_hash", "").lower()

        # Update sidebar
        self._update_or_add_conv_row(sender)

        # If currently viewing this chat, append message
        if self.current_dest_hash and self.current_dest_hash == sender:
            self.chat_view.append_message(msg_data)
            self.db.clear_unread_count(sender)
            if sender in self.conv_rows:
                conv = self.db.get_conversation(sender)
                if conv:
                    self.conv_rows[sender].update_data(conv)

    def _on_service_message_state(self, message_hash: str, state: int):
        self.chat_view.update_message_state(message_hash, state)

    def _on_service_announce_received(self, announce_data: Dict[str, Any]):
        dest_hash = announce_data["destination_hash"].lower()
        if dest_hash in self.announce_rows:
            self.announce_rows[dest_hash].update_data(announce_data)
        else:
            row = AnnounceRow(announce_data, on_start_chat=self._on_announce_start_chat)
            self.announce_rows[dest_hash] = row
            self.announce_list_box.prepend(row)

    # --- Dialog Openers ---
    def _show_new_chat_dialog(self):
        dialog = NewChatDialog(self, on_chat_created=self._on_new_chat_created)
        dialog.present()

    def _on_new_chat_created(self, dest_hash: str, nickname: Optional[str]):
        self.db.add_or_update_conversation(dest_hash=dest_hash, custom_name=nickname)
        self._load_conversations()
        self.open_conversation(dest_hash)

    def _show_profile_dialog(self):
        dialog = ProfileDialog(
            self,
            service=self.service,
            on_profile_updated=self._on_profile_updated
        )
        dialog.present()

    def _on_profile_updated(self):
        pass

    def _show_interfaces_dialog(self):
        dialog = InterfacesDialog(self, service=self.service)
        dialog.present()

    # --- Actions ---
    def _action_copy_hash(self):
        if self.current_dest_hash:
            clipboard = Gdk.Display.get_default().get_clipboard()
            clipboard.set(self.current_dest_hash)
            self._show_toast("Zieladresse kopiert!")

    def _action_rename_contact(self):
        if not self.current_dest_hash:
            return
        conv = self.db.get_conversation(self.current_dest_hash)
        current_name = (conv.get("custom_name") or conv.get("display_name") or "") if conv else ""

        # Simple input dialog
        dialog = Adw.Window(transient_for=self, modal=True)
        dialog.set_title("Kontakt umbenennen")
        dialog.set_default_size(360, 180)
        dialog.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        entry = Gtk.Entry()
        entry.set_placeholder_text("Neuer Name")
        entry.set_text(current_name)
        box.append(entry)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)

        cancel_btn = Gtk.Button(label="Abbrechen")
        cancel_btn.connect("clicked", lambda _b: dialog.close())
        btn_box.append(cancel_btn)

        save_btn = Gtk.Button(label="Speichern")
        save_btn.add_css_class("suggested-action")

        def _do_save(_b):
            new_name = entry.get_text().strip()
            self.db.set_custom_name(self.current_dest_hash, new_name)
            dialog.close()
            self._load_conversations()
            if self.current_dest_hash:
                self.open_conversation(self.current_dest_hash)
            self._show_toast("Name aktualisiert!")

        save_btn.connect("clicked", _do_save)
        entry.connect("activate", _do_save)
        btn_box.append(save_btn)

        box.append(btn_box)
        dialog.set_content(box)
        dialog.present()

    def _action_request_path(self):
        if not self.current_dest_hash:
            return
        try:
            dest_bytes = bytes.fromhex(self.current_dest_hash)
            RNS.Transport.request_path(dest_bytes)
            self._show_toast("Pfadanfrage im Mesh gesendet...")
        except Exception as e:
            self._show_toast(f"Fehler: {e}")

    def _show_toast(self, text: str):
        toast = Adw.Toast.new(text)
        toast.set_timeout(2)
        self.toast_overlay.add_toast(toast)
