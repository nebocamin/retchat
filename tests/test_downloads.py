"""File downloads from NomadNet pages: saving the response, and the page view's banner."""

import os
import time
import types

import pytest

from retchat.nomadnet_pages import (PageRequest, _FetchError, file_name_for, safe_file_name, save_download,
                                    unique_path)

NODE = bytes.fromhex("22" * 16)


def _request(path="/file/karte.pdf"):
    return PageRequest(NODE, path)


@pytest.mark.parametrize("name, expected", [
    ("karte.pdf", "karte.pdf"), ("a/b/c.zip", "c.zip"), (b"..\\x.txt", "x.txt"), (".bashrc", "bashrc"),
    ("ok\x07.txt", "ok.txt"), ("", "download"), (None, "download"),
])
def test_safe_file_name(name, expected):
    assert safe_file_name(name) == expected


def test_long_names_keep_their_extension():
    name = safe_file_name("x" * 300 + ".tar.gz")
    assert len(name) == 200 and name.endswith(".gz")


def test_unique_path(tmp_path):
    assert unique_path(str(tmp_path), "a.txt") == str(tmp_path / "a.txt")
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "a (1).txt").write_text("2")
    assert unique_path(str(tmp_path), "a.txt") == str(tmp_path / "a (2).txt")


def test_file_response_is_moved(tmp_path):
    """How RNS hands over a file response: an open temp file plus metadata."""
    temp = tmp_path / "resource"
    temp.write_bytes(b"data")
    out = tmp_path / "Downloads"
    path = save_download(open(temp, "rb"), {"name": b"../karte.pdf"}, _request(), str(out))
    assert path == str(out / "karte.pdf") and (out / "karte.pdf").read_bytes() == b"data"
    assert not temp.exists()


def test_file_response_without_name_uses_the_path(tmp_path):
    temp = tmp_path / "resource"
    temp.write_bytes(b"x")
    path = save_download(open(temp, "rb"), None, _request("/file/sub/plan.png"), str(tmp_path / "out"))
    assert os.path.basename(path) == "plan.png"


def test_old_and_plain_answers(tmp_path):
    assert open(save_download(["alt.txt", b"alt"], None, _request(), str(tmp_path)), "rb").read() == b"alt"
    path = save_download(b"roh", None, _request("/file/roh.bin"), str(tmp_path))
    assert os.path.basename(path) == "roh.bin"
    with pytest.raises(_FetchError):
        save_download({"no": "file"}, None, _request(), str(tmp_path))


def test_file_name_for():
    assert file_name_for(_request("/file/dir/karte.pdf")) == "karte.pdf"


# --- Page view ------------------------------------------------------------------------------

def _gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk
        return Gtk.init_check()
    except Exception:
        return False


class FakeDownloader:
    def __init__(self):
        self.calls, self.cancelled = [], 0

    def download(self, request, directory, on_status, on_done):
        self.calls.append((request, directory))
        self.on_status, self.on_done = on_status, on_done

    def cancel(self):
        self.cancelled += 1


@pytest.fixture
def view(tmp_path):
    if not _gtk_available():
        pytest.skip("needs a display")
    from gi.repository import Adw, Gtk
    from retchat.widgets.page_view import PageView
    Adw.init()
    service = types.SimpleNamespace(file_downloader=FakeDownloader(), downloads_dir=str(tmp_path),
                                    own_node_hex=None, node_name=lambda h: "Mesh Café",
                                    is_saved_node=lambda h: False, identifies_to=lambda h: h in identified,
                                    set_identify=lambda h, on: identified.add(h) if on else identified.discard(h),
                                    page_fetcher=types.SimpleNamespace(cancel=lambda: fetch.append("cancel"),
                                                                       fetch=lambda *a, **k: fetch.append("fetch")),
                                    app=types.SimpleNamespace(cachepath=str(tmp_path)))
    identified, fetch = set(), []
    overlay = Adw.ToastOverlay()
    page_view = PageView(service, on_back_clicked=lambda: None, on_open_chat=lambda h: None)
    overlay.set_child(page_view)
    toasts = []
    overlay.add_toast = lambda t: toasts.append(t.get_title())
    win = Adw.Window(content=overlay)
    win.present()
    page_view.identified, page_view.fetch_log = identified, fetch
    yield page_view, service.file_downloader, toasts, tmp_path
    win.destroy()


def test_download_shows_progress_and_result(view):
    from retchat.nomadnet_pages import DownloadResult
    page_view, dl, toasts, tmp = view
    request = _request()
    page_view.download(request)
    assert dl.calls == [(request, str(tmp))] and page_view.download_banner.get_revealed()
    dl.on_status("Datei wird empfangen…", 0.42)
    assert page_view.download_banner.get_title() == "«karte.pdf» 42 %"
    dl.on_status("Verbindung wird aufgebaut…", None)
    assert page_view.download_banner.get_title() == "«karte.pdf»: Verbindung wird aufgebaut…"

    page_view.download(_request("/file/zweite.zip"))  # one at a time
    assert len(dl.calls) == 1 and "läuft bereits" in toasts[-1]

    (tmp / "karte.pdf").write_bytes(b"%PDF")
    dl.on_done(DownloadResult(request, path=str(tmp / "karte.pdf")))
    assert not page_view.download_banner.get_revealed() and not page_view.downloading
    assert toasts[-1] == f"«karte.pdf» in {tmp.name} gespeichert"


def test_download_error_and_cancel(view):
    from retchat.nomadnet_pages import DownloadResult
    page_view, dl, toasts, _tmp = view
    page_view.download(_request())
    dl.on_done(DownloadResult(_request(), error="Die Verbindung zur Node wurde getrennt."))
    assert toasts[-1] == "Download fehlgeschlagen: Die Verbindung zur Node wurde getrennt."
    page_view.download(_request())
    page_view.cancel_download()
    assert dl.cancelled == 1 and not page_view.downloading and "abgebrochen" in toasts[-1]
    assert not page_view.download_banner.get_revealed()


def test_file_links_on_pages_download(view):
    from retchat.micron import Link
    page_view, dl, _toasts, _tmp = view
    page_view._on_link(Link(url=f"{NODE.hex()}:/file/karte.pdf"))
    assert dl.calls and dl.calls[0][0].path == "/file/karte.pdf"


def test_identify_menu_state_and_reload(view):
    page_view, _dl, toasts, _tmp = view
    request = PageRequest(NODE, "/page/index.mu")
    page_view.open_request(request)
    action = page_view._actions["identify"]
    assert action.get_enabled() and action.get_state().get_boolean() is False
    page_view.fetch_log.clear()
    page_view.set_identify(request, True)  # as after "Identifizieren" in the dialog
    assert page_view.identified == {NODE.hex()} and action.get_state().get_boolean() is True
    assert page_view.fetch_log == ["cancel", "fetch"]  # new link, page loaded again
    assert toasts[-1] == "Du identifizierst dich bei «Mesh Café»"
    # Switching off needs no confirmation
    action.change_state(__import__("gi.repository.GLib", fromlist=["GLib"]).Variant.new_boolean(False))
    assert page_view.identified == set() and action.get_state().get_boolean() is False
    assert toasts[-1] == "Du bist bei «Mesh Café» wieder anonym"


def test_identify_asks_first(view):
    from gi.repository import Adw, GLib
    page_view, _dl, _toasts, _tmp = view
    page_view.open_request(PageRequest(NODE, "/page/index.mu"))
    page_view._actions["identify"].activate(None)  # menu item
    dialog = page_view.get_root().get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog) and "Mesh Café" in dialog.get_body()
    assert page_view.identified == set()  # nothing until confirmed
    dialog.emit("response", "identify")
    assert page_view.identified == {NODE.hex()}
