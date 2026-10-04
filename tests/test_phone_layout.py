"""The window and the message menu fit a 360 px wide phone screen, also with larger text."""

import os
import time
from unittest import mock

import pytest

PHONE_WIDTH = 360
# The popover draws a border and an arrow around its content
MENU_MAX_CONTENT_WIDTH = PHONE_WIDTH - 24


def _gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk
        return Gtk.init_check()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _gtk_available(), reason="needs a display")

PEER = "ab" * 16


@pytest.fixture(scope="module")
def window():
    from gi.repository import Adw, Gdk, GLib, Gtk
    from retchat.window import RetchatWindow

    Adw.init()
    css = Gtk.CssProvider()
    css.load_from_path(os.path.join(os.path.dirname(os.path.dirname(__file__)), "retchat", "style.css"))
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    msgs = [{"message_hash": f"{i:064x}", "conversation_hash": PEER, "is_outgoing": i % 2 == 0,
             "content": f"Nachricht {i}", "timestamp": time.time(), "state": 2, "hops": 1} for i in (1, 2)]
    conv = {"destination_hash": PEER, "display_name": "Alice", "custom_name": None, "last_message_text": "",
            "last_message_time": time.time(), "unread_count": 0, "hops": 1}
    svc = mock.MagicMock()
    svc.get_conversations.return_value = [conv]
    svc.get_conversation.return_value = conv
    svc.get_messages.return_value = msgs
    svc.get_announces.return_value = []
    svc.display_name = "stereo"

    app = Adw.Application(application_id="org.selfmade.RetchatLayoutTest")
    app.register(None)
    win = RetchatWindow(app, mock.MagicMock(), svc)
    win.set_default_size(PHONE_WIDTH, 720)
    win.present()
    win.open_conversation(PEER)
    ctx = GLib.MainContext.default()
    end = time.monotonic() + 2
    while time.monotonic() < end:
        ctx.iteration(False)
    yield win
    win.destroy()
    Gtk.Settings.get_default().reset_property("gtk-xft-dpi")


def _set_text_scale(scale):
    from gi.repository import Gtk
    Gtk.Settings.get_default().set_property("gtk-xft-dpi", int(96 * 1024 * scale))


def _min_width(widget):
    from gi.repository import Gtk
    return widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0]


@pytest.mark.parametrize("scale", [1.0, 1.4])
def test_window_fits_phone(window, scale):
    _set_text_scale(scale)
    assert _min_width(window.get_content()) <= PHONE_WIDTH


@pytest.mark.parametrize("scale", [1.0, 1.4])
def test_message_menu_fits_phone(window, scale):
    _set_text_scale(scale)
    view = window.chat_view
    received = view._items[f"{1:064x}"]
    view._show_message_menu(received, view, 50, 50)
    try:
        assert view._reaction_choices.get_visible() and view._menu_reply_btn.get_visible()
        assert _min_width(view.reaction_popover.get_child()) <= MENU_MAX_CONTENT_WIDTH
    finally:
        view._close_reaction_picker()
