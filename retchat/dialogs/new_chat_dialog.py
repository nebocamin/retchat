"""Dialog to start a new chat by entering a Reticulum destination hash."""

from typing import Callable, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw


class NewChatDialog(Adw.Window):
    def __init__(self, parent_window: Gtk.Window, on_chat_created: Callable[[str, Optional[str]], None]):
        super().__init__(transient_for=parent_window, modal=True)

        self.on_chat_created = on_chat_created
        self.set_title("Neuer Reticulum Chat")
        self.set_default_size(440, 360)
        self.set_resizable(False)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Header Bar
        header_bar = Adw.HeaderBar()
        cancel_btn = Gtk.Button(label="Abbrechen")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header_bar.pack_start(cancel_btn)

        self.start_btn = Gtk.Button(label="Starten")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", self._on_start_clicked)
        header_bar.pack_end(self.start_btn)

        main_box.append(header_bar)

        # Content Page
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.set_title("Kontakt-Informationen")
        group.set_description(
            "Gib die 16-Byte (32 Hex-Zeichen) LXMF-Zieladresse des Kontakts ein."
        )

        # Destination Hash Entry
        self.hash_row = Adw.EntryRow(title="Ziel-Hash (Hex)")
        self.hash_row.connect("changed", self._on_input_changed)
        group.add(self.hash_row)

        # Nickname Entry
        self.name_row = Adw.EntryRow(title="Name / Nickname (optional)")
        group.add(self.name_row)

        page.add(group)

        # Error / Status message
        self.status_label = Gtk.Label()
        self.status_label.add_css_class("dim-label")
        self.status_label.set_margin_top(8)
        self.status_label.set_margin_bottom(12)
        self.status_label.set_margin_start(16)
        self.status_label.set_margin_end(16)
        self.status_label.set_wrap(True)

        main_box.append(page)
        main_box.append(self.status_label)

        self.set_content(main_box)
        self._on_input_changed(None)

    def set_prefilled_hash(self, dest_hex: str, nickname: Optional[str] = None):
        self.hash_row.set_text(dest_hex)
        if nickname:
            self.name_row.set_text(nickname)
        self._on_input_changed(None)

    def _on_input_changed(self, _entry):
        val = self.hash_row.get_text().strip().lower()
        if not val:
            self.start_btn.set_sensitive(False)
            self.status_label.set_text("")
            return

        # Validate hex
        is_hex = all(c in "0123456789abcdef" for c in val)
        if not is_hex:
            self.start_btn.set_sensitive(False)
            self.status_label.set_text("Fehler: Darf nur Hexadezimalzeichen (0-9, a-f) enthalten.")
            return

        if len(val) != 32:
            self.start_btn.set_sensitive(False)
            self.status_label.set_text(f"Länge: {len(val)}/32 Zeichen")
            return

        self.start_btn.set_sensitive(True)
        self.status_label.set_text("Gültige Zieladresse")

    def _on_start_clicked(self, _btn):
        dest_hash = self.hash_row.get_text().strip().lower()
        nickname = self.name_row.get_text().strip() or None
        self.close()
        self.on_chat_created(dest_hash, nickname)
