"""Adw.Application implementation for Retchat."""

import os
import sys

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, Gio

from retchat.database import Database
from retchat.reticulum_service import ReticulumService
from retchat.window import RetchatWindow


class RetchatApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="org.selfmade.Retchat",
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        self.db: Database = None  # type: ignore
        self.service: ReticulumService = None  # type: ignore
        self.window: RetchatWindow = None  # type: ignore

    def do_startup(self):
        Adw.Application.do_startup(self)

        # Load custom CSS
        self._load_css()

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
