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
        self.set_default_size(360, 520)
        self.set_size_request(280, 360)

        self.page = Adw.PreferencesPage()

        # 1. TCP Configuration for Mobile / Phosh
        group_tcp = Adw.PreferencesGroup()
        group_tcp.set_title("TCP-Verbindung (Standard für Mobilfunk / Phosh)")
        group_tcp.set_description(
            "Da Mobilfunk- und mobile WLAN-Netzwerke kein Multicast (AutoInterface) unterstützen, "
            "verbindet sich Retchat standardmäßig über TCP mit dem Reticulum-Netzwerk."
        )

        tcp_settings = self.service.get_tcp_settings()

        self.tcp_host_row = Adw.EntryRow(title="TCP Server / Hub")
        self.tcp_host_row.set_text(tcp_settings.get("host", "sideband.connect.reticulum.network"))
        group_tcp.add(self.tcp_host_row)

        self.tcp_port_row = Adw.EntryRow(title="Port")
        self.tcp_port_row.set_text(str(tcp_settings.get("port", "7822")))
        group_tcp.add(self.tcp_port_row)

        self.disable_auto_row = Adw.SwitchRow(title="AutoInterface (Multicast) deaktivieren")
        self.disable_auto_row.set_subtitle("Empfohlen auf Telefonen zur Vermeidung von Socket- und Übertragungsfehlern")
        self.disable_auto_row.set_active(tcp_settings.get("disable_auto", True))
        group_tcp.add(self.disable_auto_row)

        save_row = Adw.ActionRow(title="TCP-Einstellungen in ~/.reticulum/config speichern")
        save_btn = Gtk.Button(label="Speichern", valign=Gtk.Align.CENTER)
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_tcp_clicked)
        save_row.add_suffix(save_btn)
        group_tcp.add(save_row)

        self.page.add(group_tcp)

        # 2. Active Interfaces Status
        self.group = Adw.PreferencesGroup()
        self.group.set_title("Aktive Interfaces")
        self.group.set_description(
            "Übersicht aller aktuell im Reticulum-Router aktiven Schnittstellen."
        )

        self.page.add(self.group)
        self.add(self.page)

        self.refresh_interfaces()

    def _on_save_tcp_clicked(self, _btn):
        host = self.tcp_host_row.get_text().strip() or "sideband.connect.reticulum.network"
        port = self.tcp_port_row.get_text().strip() or "7822"
        disable_auto = self.disable_auto_row.get_active()

        path = self.service.save_tcp_settings(host, port, disable_auto)
        toast = Adw.Toast.new(f"Gespeichert! Bitte Retchat neu starten, um Schnittstellen neu zu laden.")
        toast.set_timeout(3)
        self.add_toast(toast)

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
