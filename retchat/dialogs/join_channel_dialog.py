"""Dialog to join a channel/room on an existing Reticulum Relay Chat (RRC) hub."""

from typing import Callable, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from retchat.reticulum_service import ReticulumService

_BaseDialog = Adw.PreferencesDialog if hasattr(Adw, "PreferencesDialog") else Adw.PreferencesWindow

POPULAR_CHANNELS = ["#general", "#talk", "#mesh", "#dev", "#test"]


class JoinChannelDialog(_BaseDialog):
    def __init__(
        self,
        parent_window: Gtk.Window,
        service: ReticulumService,
        hub_hash: str,
        hub_name: str,
        on_channel_joined: Callable[[str, str], None]
    ):
        self._parent = parent_window
        if _BaseDialog is Adw.PreferencesWindow:
            super().__init__(transient_for=parent_window, modal=True)
            self.set_default_size(340, 380)
        else:
            super().__init__()
            if hasattr(self, "set_content_width"):
                self.set_content_width(340)
                self.set_content_height(380)

        self.service = service
        self.hub_hash = hub_hash.lower()
        self.hub_name = hub_name
        self.on_channel_joined = on_channel_joined

        self.set_title("Kanal beitreten")
        self.set_size_request(280, 320)

        page = Adw.PreferencesPage()

        # Group: Hub Info & Channel Entry
        group_entry = Adw.PreferencesGroup()
        group_entry.set_title(f"Hub: {self.hub_name}")
        group_entry.set_description("Gib den Namen des Chatraums ein, dem du beitreten möchtest.")

        self.room_row = Adw.EntryRow(title="Kanalname")
        self.room_row.set_text("#general")
        self.room_row.connect("entry-activated", lambda _e: self._on_join_clicked(None))
        group_entry.add(self.room_row)

        page.add(group_entry)

        # Group: Quick Suggestions
        group_sugg = Adw.PreferencesGroup()
        group_sugg.set_title("Vorschläge")
        sugg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sugg_box.set_margin_top(6)
        sugg_box.set_margin_bottom(6)
        sugg_box.set_halign(Gtk.Align.CENTER)

        for ch in POPULAR_CHANNELS:
            btn = Gtk.Button(label=ch)
            btn.add_css_class("pill")
            btn.connect("clicked", lambda _b, c=ch: self.room_row.set_text(c))
            sugg_box.append(btn)

        group_sugg.add(sugg_box)
        page.add(group_sugg)

        # Group: Actions
        group_actions = Adw.PreferencesGroup()
        join_row = Adw.ActionRow()
        join_btn = Gtk.Button(label="Kanal beitreten", valign=Gtk.Align.CENTER)
        join_btn.add_css_class("suggested-action")
        join_btn.add_css_class("pill")
        join_btn.set_hexpand(True)
        join_btn.connect("clicked", self._on_join_clicked)
        join_row.add_suffix(join_btn)
        group_actions.add(join_row)

        close_row = Adw.ActionRow()
        close_btn = Gtk.Button(label="Abbrechen", valign=Gtk.Align.CENTER)
        close_btn.add_css_class("pill")
        close_btn.set_hexpand(True)
        close_btn.connect("clicked", lambda _b: self.close())
        close_row.add_suffix(close_btn)
        group_actions.add(close_row)

        page.add(group_actions)
        self.add(page)

    def _on_join_clicked(self, _btn):
        room = self.room_row.get_text().strip()
        if not room:
            self._show_toast("Bitte einen Kanalnamen eingeben")
            return

        clean_room = room if room.startswith("#") else "#" + room
        success = self.service.join_rrc_room(self.hub_hash, clean_room)
        if success:
            self.on_channel_joined(self.hub_hash, clean_room)
            self.close()
        else:
            self._show_toast(f"Konnte Kanal {clean_room} nicht beitreten")

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
