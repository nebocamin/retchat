"""Dialog to view Reticulum interfaces and mesh network status."""

from typing import Any, Dict, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from retchat.reticulum_service import ReticulumService

_BaseDialog = Adw.PreferencesDialog if hasattr(Adw, "PreferencesDialog") else Adw.PreferencesWindow


class InterfacesDialog(_BaseDialog):
    def __init__(self, parent_window: Gtk.Window, service: ReticulumService):
        self._parent = parent_window
        if _BaseDialog is Adw.PreferencesWindow:
            super().__init__(transient_for=parent_window, modal=True)
            self.set_default_size(380, 540)
        else:
            super().__init__()
            if hasattr(self, "set_content_width"):
                self.set_content_width(380)
                self.set_content_height(540)

        self.service = service
        self.set_title("Schnittstellen & Reticulum Status")
        self.set_size_request(300, 360)

        self.page = Adw.PreferencesPage()

        # 1. Configured Interfaces with On/Off Toggles
        self.group_ifaces = Adw.PreferencesGroup()
        self.group_ifaces.set_title("Mesh-Schnittstellen (An / Aus)")
        self.group_ifaces.set_description(
            "Schalte Schnittstellen flexibel an oder aus. "
            "Deaktivierte RNodes werden sofort getrennt und blockieren keine USB-Ports."
        )
        self.page.add(self.group_ifaces)
        self._build_interface_toggles()

        # 2. TCP Hub Configuration
        group_tcp = Adw.PreferencesGroup()
        group_tcp.set_title("TCP Hub / Server")
        group_tcp.set_description(
            "Verbindung zu einem Reticulum-Hub über das Internet oder Mobilfunknetz."
        )

        tcp_settings = self.service.get_tcp_settings()

        self.tcp_host_row = Adw.EntryRow(title="TCP Server / Host")
        self.tcp_host_row.set_text(tcp_settings.get("host", "sideband.connect.reticulum.network"))
        group_tcp.add(self.tcp_host_row)

        self.tcp_port_row = Adw.EntryRow(title="Port")
        self.tcp_port_row.set_text(str(tcp_settings.get("port", "7822")))
        group_tcp.add(self.tcp_port_row)

        save_row = Adw.ActionRow(title="TCP-Knoten speichern")
        save_btn = Gtk.Button(label="Speichern", valign=Gtk.Align.CENTER)
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_tcp_clicked)
        save_row.add_suffix(save_btn)
        group_tcp.add(save_row)

        self.page.add(group_tcp)

        # 3. Mesh Presence & Announce
        group_announce = Adw.PreferencesGroup()
        group_announce.set_title("Mesh-Präsenz (Announce)")
        group_announce.set_description(
            "Kündigt deine Zieladresse im Reticulum-Netzwerk an, damit Peers dich finden können."
        )

        announce_row = Adw.ActionRow(
            title="Jetzt im Mesh ankündigen",
            subtitle=f"Anzeigename: {self.service.display_name or 'Unbenannt'}"
        )
        announce_btn = Gtk.Button(label="Ankündigen", valign=Gtk.Align.CENTER)
        announce_btn.add_css_class("suggested-action")
        announce_btn.connect("clicked", self._on_announce_clicked)
        announce_row.add_suffix(announce_btn)
        group_announce.add(announce_row)

        self.page.add(group_announce)

        # 4. Live Interfaces Status
        self.group_live = Adw.PreferencesGroup()
        self.group_live.set_title("Live-Verkehrszähler")
        self.group_live.set_description(
            "Übersicht der Datenmengen auf aktiven Schnittstellen."
        )
        self.page.add(self.group_live)
        self._refresh_live_stats()

        # 5. Close / Done button (especially critical for mobile Phosh navigation)
        group_close = Adw.PreferencesGroup()
        close_row = Adw.ActionRow()
        close_btn = Gtk.Button(label="Schließen", valign=Gtk.Align.CENTER)
        close_btn.add_css_class("suggested-action")
        close_btn.add_css_class("pill")
        close_btn.set_hexpand(True)
        close_btn.connect("clicked", lambda _b: self.close())
        close_row.add_suffix(close_btn)
        group_close.add(close_row)
        self.page.add(group_close)

        self.add(self.page)

    def _build_interface_toggles(self):
        configured = self.service.get_configured_interfaces()
        if not configured:
            empty_row = Adw.ActionRow(title="Keine Schnittstellen in ~/.reticulum/config gefunden")
            self.group_ifaces.add(empty_row)
            return

        for iface in configured:
            name = iface["name"]
            itype = iface["type"]
            enabled = iface["enabled"]
            online = iface["online"]
            details = iface["details"]

            row = Adw.SwitchRow()
            row.set_title(name)
            row.set_subtitle(f"{itype} • {details}")
            row.set_active(enabled)

            # Status Badge
            badge = Gtk.Label()
            badge.add_css_class("badge")
            self._update_badge(badge, enabled, online)
            row.add_suffix(badge)

            # Connect toggle change
            def _on_toggled(switch, _pspec, iface_name=name, status_badge=badge):
                is_active = switch.get_active()
                success = self.service.set_interface_enabled(iface_name, is_active)
                if success:
                    self._update_badge(status_badge, is_active, False if not is_active else online)
                    if is_active:
                        self._show_toast(f"'{iface_name}' aktiviert (nach Neustart aktiv)")
                    else:
                        self._show_toast(f"'{iface_name}' deaktiviert und getrennt")
                    # Refresh live list
                    self._refresh_live_stats()
                else:
                    self._show_toast(f"Fehler beim Ändern von '{iface_name}'")

            row.connect("notify::active", _on_toggled)
            self.group_ifaces.add(row)

    def _update_badge(self, badge: Gtk.Label, enabled: bool, online: bool):
        badge.remove_css_class("hops-badge")
        badge.remove_css_class("unread-badge")
        badge.remove_css_class("dim-label")

        if not enabled:
            badge.set_text("Aus")
            badge.add_css_class("dim-label")
        elif online:
            badge.set_text("Online")
            badge.add_css_class("hops-badge")
        else:
            badge.set_text("Warten")
            badge.add_css_class("unread-badge")

    def _refresh_live_stats(self):
        interfaces = self.service.get_interfaces_info()
        if not interfaces:
            row = Adw.ActionRow(title="Keine aktiven Verbindungen")
            self.group_live.add(row)
            return

        for iface in interfaces:
            name = iface.get("name") or "Unbenannt"
            itype = iface.get("type", "Interface")
            online = iface.get("online", False)
            rxb = iface.get("rxb", 0)
            txb = iface.get("txb", 0)

            row = Adw.ActionRow()
            row.set_title(name)
            row.set_subtitle(f"RX: {self._format_bytes(rxb)} • TX: {self._format_bytes(txb)}")

            status_label = Gtk.Label(label="Aktiv" if online else "Inaktiv")
            status_label.add_css_class("badge")
            if online:
                status_label.add_css_class("hops-badge")
            else:
                status_label.add_css_class("dim-label")
            row.add_suffix(status_label)
            self.group_live.add(row)

    def _on_save_tcp_clicked(self, _btn):
        host = self.tcp_host_row.get_text().strip() or "sideband.connect.reticulum.network"
        port = self.tcp_port_row.get_text().strip() or "7822"

        self.service.save_tcp_settings(host, port, False)
        self._show_toast("TCP-Einstellungen gespeichert!")

    def _on_announce_clicked(self, _btn):
        try:
            self.service.announce()
            self._show_toast("Announce wurde im Reticulum-Mesh gesendet!")
        except Exception as e:
            self._show_toast(f"Fehler beim Announce: {e}")

    def _show_toast(self, message: str, timeout: int = 3):
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self.add_toast(toast)

    def _format_bytes(self, num_bytes: int) -> str:
        if num_bytes < 1024:
            return f"{num_bytes} B"
        elif num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024:.1f} KB"
        else:
            return f"{num_bytes / (1024 * 1024):.1f} MB"

    def present(self, parent: Optional[Gtk.Widget] = None):
        target = parent or self._parent
        if hasattr(Adw, "PreferencesDialog") and isinstance(self, Adw.PreferencesDialog):
            super().present(target)
        else:
            super().present()

