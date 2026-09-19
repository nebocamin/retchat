"""Dialog to view Reticulum interfaces and mesh network status."""

from typing import Any, Dict, List

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from retchat.reticulum_service import ReticulumService


class InterfacesDialog(Adw.PreferencesWindow):
    def __init__(self, parent_window: Gtk.Window, service: ReticulumService):
        super().__init__(transient_for=parent_window, modal=True)

        self.service = service
        self.set_title("Reticulum Schnittstellen & Status")
        self.set_default_size(540, 520)

        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup()
        self.group.set_title("Aktive Interfaces")
        self.group.set_description(
            "Übersicht aller in ~/.reticulum/config konfigurierten Netzwerk- und Funk-Schnittstellen."
        )

        self.page.add(self.group)
        self.add(self.page)

        self.refresh_interfaces()

    def refresh_interfaces(self):
        # Clear existing rows
        while child := self.group.get_first_child():
            self.group.remove(child)

        interfaces = self.service.get_interfaces_info()

        if not interfaces:
            empty_row = Adw.ActionRow(title="Keine aktiven Schnittstellen gefunden")
            self.group.add(empty_row)
            return

        for iface in interfaces:
            name = iface.get("name") or "Unbenannt"
            itype = iface.get("type", "Interface")
            online = iface.get("online", False)
            rxb = iface.get("rxb", 0)
            txb = iface.get("txb", 0)

            row = Adw.ActionRow()
            row.set_title(name)
            row.set_subtitle(f"Typ: {itype} • RX: {self._format_bytes(rxb)} / TX: {self._format_bytes(txb)}")

            # Status pill / icon
            status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            status_box.set_valign(Gtk.Align.CENTER)

            status_label = Gtk.Label(label="Online" if online else "Offline")
            status_label.add_css_class("badge")
            if online:
                status_label.add_css_class("hops-badge")
            else:
                status_label.add_css_class("unread-badge")

            status_box.append(status_label)
            row.add_suffix(status_box)

            self.group.add(row)

    def _format_bytes(self, num_bytes: int) -> str:
        if num_bytes < 1024:
            return f"{num_bytes} B"
        elif num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024:.1f} KB"
        else:
            return f"{num_bytes / (1024 * 1024):.1f} MB"
