"""Adw.Application implementation for Retchat."""

import os
import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, Gio

from retchat.database import Database
from retchat.reticulum_service import ReticulumService
from retchat.widgets.attachment_row import purge_open_copies
from retchat.window import RetchatWindow

APP_ID = "org.selfmade.Retchat"
VERSION = "0.1.0"
# Icons of a source checkout; installed builds (Flatpak) use /app/share/icons.
SOURCE_ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "icons")


class RetchatApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        self.db: Database = None  # type: ignore
        self.service: ReticulumService = None  # type: ignore
        self.window: RetchatWindow = None  # type: ignore

    def do_startup(self):
        Adw.Application.do_startup(self)

        # Load custom CSS
        self._load_css()
        self._setup_icons()
        purge_open_copies()  # copies made for opening attachments in other apps

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", lambda _a, _p: self._show_about())
        self.add_action(about_action)

        # Initialize Database and Reticulum Service
        self.db = Database()
        self.service = ReticulumService(self.db)

    def _load_css(self):
        css_path = os.path.join(os.path.dirname(__file__), "style.css")
        if os.path.exists(css_path):
            provider = Gtk.CssProvider()
            provider.load_from_path(css_path)
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(
                    display,
                    provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )

    def _setup_icons(self):
        display = Gdk.Display.get_default()
        if display and os.path.isdir(SOURCE_ICON_DIR):
            # Prepend, so the checkout's icon wins over an installed (older) copy.
            theme = Gtk.IconTheme.get_for_display(display)
            theme.set_search_path([SOURCE_ICON_DIR, *theme.get_search_path()])
        Gtk.Window.set_default_icon_name(APP_ID)

    def _show_about(self):
        about = Adw.AboutDialog(
            application_name="Retchat",
            application_icon=APP_ID,
            version=VERSION,
            developer_name="stereo",
            comments="Dezentraler, Ende-zu-Ende-verschlüsselter Chat über das Reticulum-Mesh-Netzwerk (LXMF).",
            website="https://github.com/nebocamin/retchat",
            issue_url="https://github.com/nebocamin/retchat/issues",
            license_type=Gtk.License.GPL_3_0,
        )
        about.add_link("Reticulum Network", "https://reticulum.network/")
        about.present(self.get_active_window())

    def do_activate(self):
        if not self.window:
            self.window = RetchatWindow(self, self.db, self.service)
        self.window.present()

    def do_shutdown(self):
        if self.service:
            self.service.shutdown()
        Adw.Application.do_shutdown(self)


def main():
    app = RetchatApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
