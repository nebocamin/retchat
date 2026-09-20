"""Profile and Identity dialog."""

from typing import Callable

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk

from retchat.reticulum_service import ReticulumService


class ProfileDialog(Adw.PreferencesWindow):
    def __init__(self, parent_window: Gtk.Window, service: ReticulumService, on_profile_updated: Callable[[], None]):
        super().__init__(transient_for=parent_window, modal=True)

        self.service = service
        self.on_profile_updated = on_profile_updated
        self.set_title("Eigene Identität & Profil")
        self.set_default_size(360, 520)
        self.set_size_request(280, 360)

        page = Adw.PreferencesPage()

        # Group 1: Profile
        group_profile = Adw.PreferencesGroup()
        group_profile.set_title("Profil im Mesh-Netzwerk")
        group_profile.set_description(
            "Dein Anzeigename wird bei Ankündigungen (Announces) im Reticulum-Netzwerk mitgesendet."
        )

        self.name_row = Adw.EntryRow(title="Anzeigename")
        self.name_row.set_text(self.service.display_name)
        group_profile.add(self.name_row)

        save_row = Adw.ActionRow(title="Namen speichern & im Mesh ankündigen")
        save_btn = Gtk.Button(label="Speichern", valign=Gtk.Align.CENTER)
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_name_clicked)
        save_row.add_suffix(save_btn)
        group_profile.add(save_row)

        page.add(group_profile)

        # Group 2: Reticulum Hashes
        group_hashes = Adw.PreferencesGroup()
        group_hashes.set_title("Deine Adressen")
        group_hashes.set_description(
            "Teile deine LXMF-Zieladresse mit anderen, damit sie dir verschlüsselte Nachrichten senden können."
        )

        # LXMF Delivery Destination Hash
        self.dest_row = Adw.ActionRow(
            title="LXMF Zieladresse",
            subtitle=self.service.delivery_destination_hex
        )
        self.dest_row.set_subtitle_selectable(True)
        copy_dest_btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy_dest_btn.set_tooltip_text("Zieladresse kopieren")
        copy_dest_btn.connect("clicked", lambda _b: self._copy_to_clipboard(self.service.delivery_destination_hex))
        self.dest_row.add_suffix(copy_dest_btn)
        group_hashes.add(self.dest_row)

        # Reticulum Identity Hash
        self.id_row = Adw.ActionRow(
            title="Reticulum Identitäts-Hash",
            subtitle=self.service.identity_hex
        )
        self.id_row.set_subtitle_selectable(True)
        copy_id_btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy_id_btn.set_tooltip_text("Identitäts-Hash kopieren")
        copy_id_btn.connect("clicked", lambda _b: self._copy_to_clipboard(self.service.identity_hex))
        self.id_row.add_suffix(copy_id_btn)
        group_hashes.add(self.id_row)

        page.add(group_hashes)

        # Group 3: Announce
        group_announce = Adw.PreferencesGroup()
        group_announce.set_title("Mesh-Präsenz")
        group_announce.set_description(
            "Ein Announce informiert alle erreichbaren Knoten und Kontakte über deine Zieladresse."
        )

        announce_row = Adw.ActionRow(title="Manuelle Ankündigung (Announce)")
        self.announce_btn = Gtk.Button(label="Jetzt ankündigen", valign=Gtk.Align.CENTER)
        self.announce_btn.connect("clicked", self._on_announce_clicked)
        announce_row.add_suffix(self.announce_btn)
        group_announce.add(announce_row)

        page.add(group_announce)

        self.add(page)

    def _on_save_name_clicked(self, _btn):
        new_name = self.name_row.get_text().strip()
        if new_name:
            self.service.set_display_name(new_name, announce_now=True)
            self.on_profile_updated()
            self._show_toast(f"Profil gespeichert und als '{new_name}' im Mesh angekündigt!")

    def _on_announce_clicked(self, _btn):
        self.service.announce()
        self._show_toast("Announce wurde im Reticulum-Mesh gesendet!")

    def _copy_to_clipboard(self, text: str):
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(text)
        self._show_toast("In die Zwischenablage kopiert!")

    def _show_toast(self, message: str):
        toast = Adw.Toast.new(message)
        toast.set_timeout(2)
        self.add_toast(toast)
