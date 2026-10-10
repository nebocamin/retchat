"""Sidebar tab "Seiten" and the star in the page view."""

import time
from unittest import mock

import pytest


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

NODE = "22" * 16


class Nodes:
    """In-memory stand-in for the saved node methods of ReticulumService."""

    def __init__(self):
        self.saved = {}

    def saved_nodes(self):
        return [{"destination_hash": h, "name": n, "hops": 1} for h, n in sorted(self.saved.items())]

    def is_saved_node(self, h):
        return h in self.saved

    def save_node(self, h, name=None):
        self.saved[h] = name or "Mesh Café"

    def forget_node(self, h):
        return self.saved.pop(h, None)

    def rename_node(self, h, name):
        self.saved[h] = name


@pytest.fixture(scope="module")
def app():
    from gi.repository import Adw
    Adw.init()
    application = Adw.Application(application_id="org.selfmade.RetchatPagesTest")
    application.register(None)
    return application


@pytest.fixture
def window(app):
    from gi.repository import GLib
    from retchat.window import RetchatWindow
    nodes = Nodes()
    svc = mock.MagicMock()
    svc.get_conversations.return_value = []
    svc.get_announces.return_value = []
    svc.display_name = "me"
    svc.own_node_hex = None
    svc.node_name.return_value = "Mesh Café"
    for name in ("saved_nodes", "is_saved_node", "save_node", "forget_node", "rename_node"):
        setattr(svc, name, getattr(nodes, name))
    win = RetchatWindow(app, mock.MagicMock(), svc)
    win.present()

    def spin(sec=0.3):
        ctx = GLib.MainContext.default()
        end = time.monotonic() + sec
        while time.monotonic() < end:
            ctx.iteration(False)
    spin()
    yield win, nodes, spin
    win.destroy()


def _rows(list_box):
    out, i = [], 0
    while row := list_box.get_row_at_index(i):
        out.append(row.dest_hash)
        i += 1
    return out


def test_three_tabs(window):
    win, _nodes, _spin = window
    pages = win.sidebar_stack.get_pages()
    names = [pages.get_item(i).get_name() for i in range(pages.get_n_items())]
    titles = [pages.get_item(i).get_title() for i in range(pages.get_n_items())]
    assert names == ["chats", "pages", "discover"] and titles == ["Chats", "Seiten", "Entdecken"]


def test_star_saves_the_node_and_lists_it(window):
    win, nodes, spin = window
    win.sidebar_stack.set_visible_child_name("pages")
    spin()
    assert _rows(win.nodes_list_box) == []
    win.open_page(NODE)
    view = win.page_view
    assert view.save_btn.get_icon_name() == "non-starred-symbolic"
    view.activate_action("page.save-node", None)
    assert nodes.saved == {NODE: "Mesh Café"}
    assert view.save_btn.get_icon_name() == "starred-symbolic"
    assert _rows(win.nodes_list_box) == [NODE]
    view.activate_action("page.save-node", None)  # Ctrl+D again: removed
    assert nodes.saved == {} and _rows(win.nodes_list_box) == []


def test_remove_from_the_list_with_undo(window):
    win, nodes, spin = window
    nodes.save_node(NODE, "Café")
    win.open_page(NODE)
    win._load_saved_nodes()
    from gi.repository import GLib
    win.activate_action("win.forget-node", GLib.Variant.new_string(NODE))
    assert nodes.saved == {} and _rows(win.nodes_list_box) == []
    assert win.page_view.save_btn.get_icon_name() == "non-starred-symbolic"  # star follows


def test_search_filters_saved_nodes(window):
    win, nodes, spin = window
    nodes.save_node(NODE, "Mesh Café")
    nodes.save_node("33" * 16, "Wetter")
    win.sidebar_stack.set_visible_child_name("pages")
    win.search_entry.set_text("wett")
    spin(0.6)  # search-changed is delayed
    assert _rows(win.nodes_list_box) == ["33" * 16]
