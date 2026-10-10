"""The "+" dialog recognises what an address is and opens it as chat or page."""

import time

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

CONTACT, NODE, PROP, IDENT, UNKNOWN = ("11" * 16, "22" * 16, "33" * 16, "44" * 16, "55" * 16)


class FakeService:
    delivery_destination_hex = "ff" * 16
    display_name = "me"
    contact_uri = "lxmf://" + "ff" * 16

    def __init__(self):
        self.kinds = {CONTACT: ("contact", "Alice"), NODE: ("node", "Mesh Café"),
                      PROP: ("propagation", None), IDENT: ("identity", None)}
        self.path_requests = []

    def address_kind(self, h):
        return self.kinds.get(h, ("unknown", None))

    def addresses_for_identity(self, h):
        return {"contact": CONTACT, "node": NODE}

    def node_name(self, h):
        return "Mesh Café" if h == NODE else None

    def request_path(self, h):
        self.path_requests.append(h)

    def learn_contact(self, c):
        return True


@pytest.fixture
def dialog():
    from gi.repository import Adw, Gtk
    from retchat.dialogs.new_chat_dialog import NewChatDialog
    Adw.init()
    chats, pages = [], []
    service = FakeService()
    win = Gtk.Window()
    d = NewChatDialog(win, lambda h, n: chats.append((h, n)), service=service,
                      on_open_page=pages.append)
    yield d, service, chats, pages
    d.destroy()
    win.destroy()


def _state(d):
    return d.kind, d.start_btn.get_label(), d.start_btn.get_sensitive(), d.name_row.get_visible()


def test_contact(dialog):
    d, _s, chats, pages = dialog
    d.hash_row.set_text(CONTACT)
    assert _state(d) == ("contact", "Chat starten", True, True)
    assert d.status_label.get_text() == "Kontakt: Alice"
    d.name_row.set_text("Ali")
    d._on_start_clicked(None)
    assert chats == [(CONTACT, "Ali")] and pages == []


def test_node_is_only_opened(dialog):
    d, _s, chats, pages = dialog
    d.hash_row.set_text(NODE)
    assert _state(d) == ("node", "Seite öffnen", True, False)
    assert d.status_label.get_text() == "NomadNet-Seite: Mesh Café"
    d._on_start_clicked(None)
    assert pages == [NODE] and chats == []


def test_page_link(dialog):
    d, _s, _chats, pages = dialog
    d.hash_row.set_text(f"nomadnetwork://{NODE}:/page/about.mu")
    assert _state(d) == ("page", "Seite öffnen", True, False)
    assert "about.mu" in d.status_label.get_text()
    d._on_start_clicked(None)
    assert pages == [f"{NODE}:/page/about.mu"]


def test_propagation_node_cant_be_opened(dialog):
    d, *_ = dialog
    d.hash_row.set_text(PROP)
    assert _state(d)[:3] == ("propagation", "Öffnen", False)


def test_identity_hash_asks_and_resolves(dialog):
    d, _s, chats, pages = dialog
    d.hash_row.set_text(IDENT)
    assert d.kind == "choose" and d.kind_row.get_visible()
    assert d.start_btn.get_label() == "Chat starten"
    d.kind_toggle.set_active_name("page")
    assert _state(d) == ("choose", "Seite öffnen", True, False)
    d._on_start_clicked(None)
    assert pages == [NODE]  # the identity's node address


def test_unknown_is_searched_then_recognised(dialog, monkeypatch):
    from gi.repository import GLib
    d, s, chats, _pages = dialog
    d.hash_row.set_text(UNKNOWN)
    assert d.kind == "searching" and not d.start_btn.get_sensitive() and d.search_spinner.get_visible()
    assert s.path_requests == [UNKNOWN]
    s.kinds[UNKNOWN] = ("contact", "Bob")  # its announce arrives
    ctx = GLib.MainContext.default()
    end = time.monotonic() + 3
    while d.kind == "searching" and time.monotonic() < end:
        ctx.iteration(False)
    assert d.kind == "contact" and d.status_label.get_text() == "Kontakt: Bob"


def test_unknown_not_found_lets_the_user_choose(dialog, monkeypatch):
    import retchat.dialogs.new_chat_dialog as ncd
    from gi.repository import GLib
    monkeypatch.setattr(ncd, "SEARCH_SECONDS", 1)
    d, _s, chats, _pages = dialog
    d.hash_row.set_text(UNKNOWN)
    ctx = GLib.MainContext.default()
    end = time.monotonic() + 5
    while d.kind == "searching" and time.monotonic() < end:
        ctx.iteration(False)
    assert d.kind == "choose" and d.kind_row.get_visible() and d.start_btn.get_sensitive()
    d._on_start_clicked(None)
    assert chats == [(UNKNOWN, None)]


def test_typing_stops_the_search(dialog):
    d, *_ = dialog
    d.hash_row.set_text(UNKNOWN)
    assert d._search_source
    d.hash_row.set_text(CONTACT)
    assert not d._search_source and d.kind == "contact"


def test_scanned_page_link(dialog):
    d, *_ = dialog
    d._on_scanned(f"nomadnetwork://{NODE}:/page/index.mu")
    assert d.kind == "page"


def test_parse_target():
    from retchat.contact_uri import Contact
    from retchat.dialogs.new_chat_dialog import parse_target
    from retchat.nomadnet_pages import PageRequest
    assert isinstance(parse_target(f"{NODE}:/page/x.mu"), PageRequest)
    assert parse_target(f"lxmf://{CONTACT}") == Contact(CONTACT)
    with pytest.raises(ValueError):
        parse_target(f"nomadnetwork://{NODE}:/file/x.zip")
