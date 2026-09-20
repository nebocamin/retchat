"""Dialog to add an RRC (Reticulum Relay Chat) hub."""

from typing import Callable, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from retchat.reticulum_service import ReticulumService

_BaseDialog = Adw.PreferencesDialog if hasattr(Adw, "PreferencesDialog") else Adw.PreferencesWindow

# Community public hub suggestion
COMMUNITY_HUB_HASH = "28c7c1a68c735693aa8e6b8193ed44b2"
COMMUNITY_HUB_NAME = "rns.recipes Community Hub"


class AddRelayDialog(_BaseDialog):
    def __init__(self, parent_window: Gtk.Window, service: ReticulumService, on_hub_added: Callable[[str, Optional[str]], None]):
        self._parent = parent_window
        if _BaseDialog is Adw.PreferencesWindow:
            super().__init__(transient_for=parent_window, modal=True)
            self.set_default_size(360, 480)
        else:
            super().__init__()
            if hasattr(self, "set_content_width"):
                self.set_content_width(360)
                self.set_content_height(480)

        self.service = service
        self.on_hub_added = on_hub_added
        self.set_title("Relay-Hub beitreten")
        self.set_size_request(280, 360)

        page = Adw.PreferencesPage()

        # 1. Quick Add Community Hub
        group_quick = Adw.PreferencesGroup()
        group_quick.set_title("Bekannte Relay-Knoten")
        group_quick.set_description(
            "Öffentliche Community-Hubs zum Chatten im Reticulum-Mesh."
        )

        quick_row = Adw.ActionRow(
            title=COMMUNITY_HUB_NAME,
            subtitle=f"{COMMUNITY_HUB_HASH[:16]}..."
        )
        quick_btn = Gtk.Button(label="Hinzufügen", valign=Gtk.Align.CENTER)
        quick_btn.add_css_class("suggested-action")
        quick_btn.connect("clicked", self._on_quick_join_clicked)
        quick_row.add_suffix(quick_btn)
        group_quick.add(quick_row)
        page.add(group_quick)

        # 2. Custom Hub
        group_custom = Adw.PreferencesGroup()
        group_custom.set_title("Hub-Adresse eingeben")
        group_custom.set_description(
            "Trage die 32-stellige Hex-Zieladresse eines Reticulum Relay Chat Hubs ein."
        )

        self.hash_row = Adw.EntryRow(title="Hub Ziel-Hash (32 Hex-Zeichen)")
        group_custom.add(self.hash_row)

        self.name_row = Adw.EntryRow(title="Name / Bezeichnung (optional)")
        group_custom.add(self.name_row)

        self.room_row = Adw.EntryRow(title="Erster Kanal (optional)")
        self.room_row.set_placeholder_text("z. B. #general (oder leer lassen)")
        group_custom.add(self.room_row)

        save_row = Adw.ActionRow(title="Hub speichern und verbinden")
        save_btn = Gtk.Button(label="Verbinden", valign=Gtk.Align.CENTER)
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_custom_join_clicked)
        save_row.add_suffix(save_btn)
        group_custom.add(save_row)

        page.add(group_custom)

        # 3. Close Button
        group_close = Adw.PreferencesGroup()
        close_row = Adw.ActionRow()
        close_btn = Gtk.Button(label="Schließen", valign=Gtk.Align.CENTER)
        close_btn.add_css_class("pill")
        close_btn.set_hexpand(True)
        close_btn.connect("clicked", lambda _b: self.close())
        close_row.add_suffix(close_btn)
        group_close.add(close_row)
        page.add(group_close)

        self.add(page)

    def _on_quick_join_clicked(self, _btn):
        success, msg = self.service.add_rrc_hub(
            COMMUNITY_HUB_HASH,
            name=COMMUNITY_HUB_NAME,
            initial_room=None
        )
        if success:
            self.on_hub_added(COMMUNITY_HUB_HASH, None)
            self.close()
        else:
            self._show_toast(msg)

    def _on_custom_join_clicked(self, _btn):
        hub_hex = self.hash_row.get_text().strip()
        name = self.name_row.get_text().strip() or None
        room = self.room_row.get_text().strip() or None

        if not hub_hex:
            self._show_toast("Bitte einen Hub-Hash eingeben")
            return

        clean_room = None
        if room:
            clean_room = room if room.startswith("#") else "#" + room

        success, msg = self.service.add_rrc_hub(hub_hex, name=name, initial_room=clean_room)
        if success:
            self.on_hub_added(hub_hex.lower(), clean_room)
            self.close()
        else:
            self._show_toast(msg)

    def _show_toast(self, message: str):
        toast = Adw.Toast.new(message)
        toast.set_timeout(3)
        self.add_toast(toast)

    def present(self, parent: Optional[Gtk.Widget] = None):
        target = parent or self._parent
        if hasattr(Adw, "PreferencesDialog") and isinstance(self, Adw.PreferencesDialog):
            super().present(target)
        else:
            super().present()
