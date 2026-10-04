"""Contact links and QR codes: format, verification, reading from images and the camera pipeline."""

import os
import time
import types

import pytest
import RNS

from retchat.contact_uri import Contact, contact_uri, delivery_hash_for_key, parse_contact
from retchat.qr import decode_gray, decode_image_file, qr_matrix

IDENTITY = RNS.Identity()
KEY = IDENTITY.get_public_key()
DEST = RNS.Destination.hash(IDENTITY, "lxmf", "delivery").hex()


# --- Links ------------------------------------------------------------------------

def test_lxma_link_round_trip():
    uri = contact_uri(DEST, KEY)
    assert uri == f"lxma://{DEST}:{KEY.hex()}"  # as MeshChatX writes it
    assert parse_contact(uri) == Contact(DEST, KEY)
    assert parse_contact(f"  {uri.upper()}\n") == Contact(DEST, KEY)


@pytest.mark.parametrize("text", [DEST, DEST.upper(), f"lxmf://{DEST}", f"lxmf://{DEST}/", f"lxmf:{DEST}",
                                  f"lxmf@{DEST}", f"{DEST[:16]} {DEST[16:]}"])
def test_address_forms(text):
    assert parse_contact(text) == Contact(DEST)


def test_link_with_foreign_key_is_rejected():
    other = RNS.Identity().get_public_key()
    with pytest.raises(ValueError, match="passen nicht"):
        parse_contact(f"lxma://{DEST}:{other.hex()}")


def test_short_key_uses_address_only():
    assert parse_contact(f"lxma://{DEST}:{KEY[:32].hex()}") == Contact(DEST)


@pytest.mark.parametrize("text, message", [
    ("", "Keine Adresse"),
    ("abc123", "32 Hex-Zeichen"),
    ("https://example.org", "Keine LXMF-Adresse"),
    ("lxm://AbCd", "Papier-Nachricht"),
    (f"nomadnetwork://{DEST}:/page/index.mu", "NomadNet-Seite"),
    (f"lxma://{DEST}:zz", "Keine LXMF-Adresse"),
    (f"lxma://{DEST}:{'ab' * 10}", "Ungültiger Schlüssel"),
])
def test_invalid_input(text, message):
    with pytest.raises(ValueError, match=message):
        parse_contact(text)


def test_delivery_hash_matches_rns():
    assert delivery_hash_for_key(KEY) == DEST


# --- QR codes ---------------------------------------------------------------------

def _render(matrix, scale=4):
    """Grayscale bytes of a QR matrix, rows padded to 4 bytes like video frames."""
    size = len(matrix) * scale
    stride = (size + 3) // 4 * 4
    rows = []
    for row in matrix:
        line = bytes(0 if dark else 255 for dark in row for _ in range(scale)).ljust(stride, b"\x80")
        rows.extend([line] * scale)
    return b"".join(rows), size, stride


def test_qr_round_trip_through_a_padded_frame():
    uri = contact_uri(DEST, KEY)
    matrix = qr_matrix(uri)
    assert len(matrix) == 53 + 2 * 4  # version 9 with quiet zone: small enough to scan from a screen
    data, size, stride = _render(matrix)
    assert decode_gray(data, size, size, stride) == [uri]
    assert decode_gray(data[:10], size, size, stride) == []  # truncated frame


def test_decode_image_file(tmp_path):
    from PIL import Image
    uri = contact_uri(DEST, KEY)
    data, size, stride = _render(qr_matrix(uri), scale=6)
    path = tmp_path / "shot.png"
    Image.frombytes("L", (stride, size), data).save(path)
    assert decode_image_file(str(path)) == [uri]
    Image.new("L", (100, 100), 255).save(tmp_path / "empty.png")
    assert decode_image_file(str(tmp_path / "empty.png")) == []


# --- Service ------------------------------------------------------------------------

@pytest.fixture
def service():
    from retchat.reticulum_service import ReticulumService
    return ReticulumService.__new__(ReticulumService)


def test_learn_contact_remembers_unknown_identity(service, monkeypatch):
    known = {}
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: known.get(h)))
    monkeypatch.setattr(RNS.Identity, "remember",
                        staticmethod(lambda ph, h, key, app_data=None: known.__setitem__(h, key)))
    assert service.learn_contact(Contact(DEST)) is False  # address only, nothing to learn
    assert service.learn_contact(Contact(DEST, KEY)) is True
    assert known == {bytes.fromhex(DEST): KEY}


def test_learn_contact_keeps_known_identity(service, monkeypatch):
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: IDENTITY))
    calls = []
    monkeypatch.setattr(RNS.Identity, "remember", staticmethod(lambda *a, **k: calls.append(a)))
    assert service.learn_contact(Contact(DEST, KEY)) is True
    assert calls == []  # would drop the announced name


def test_own_contact_uri(service, monkeypatch):
    service.app = types.SimpleNamespace(identity=IDENTITY, lxmf_destination=types.SimpleNamespace(
        hash=bytes.fromhex(DEST)))
    assert parse_contact(service.contact_uri) == Contact(DEST, KEY)


# --- Camera pipeline (with an image instead of a camera) ------------------------------------

def _gst_available():
    try:
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gst, Gtk
        Gst.init(None)
        needed = ("filesrc", "pngdec", "imagefreeze", "videoconvert", "tee", "appsink", "gtk4paintablesink")
        return Gtk.init_check() and all(Gst.ElementFactory.find(e) for e in needed)
    except Exception:
        return False


@pytest.mark.skipif(not _gst_available(), reason="needs GStreamer (gtk4paintablesink) and a display")
def test_scanner_pipeline_reads_qr(tmp_path):
    from PIL import Image
    from gi.repository import GLib
    from retchat.widgets.qr_scanner import QrScanner

    uri = contact_uri(DEST, KEY)
    data, size, stride = _render(qr_matrix(uri), scale=5)
    frame = Image.new("L", (640, 480), 210)
    frame.paste(Image.frombytes("L", (stride, size), data).rotate(5, fillcolor=210), (150, 60))
    path = tmp_path / "frame.png"
    frame.convert("RGB").save(path)

    found = []
    scanner = QrScanner(found.append,
                        source=f"filesrc location={path} ! pngdec ! imagefreeze ! video/x-raw,framerate=10/1")
    scanner.start()
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 10
    while not found and time.monotonic() < deadline:
        ctx.iteration(False)
        time.sleep(0.01)
    assert scanner.running and scanner.picture.get_paintable() is not None
    scanner.stop()
    assert found and found[0] == uri
    assert not scanner.running and scanner.picture.get_paintable() is None
