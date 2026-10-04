"""Widget showing a QR code, sharp at any size."""

from typing import List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Graphene', '1.0')
from gi.repository import Gdk, Graphene, Gtk

from retchat.qr import qr_matrix

_WHITE = Gdk.RGBA()
_WHITE.parse("#ffffff")
_BLACK = Gdk.RGBA()
_BLACK.parse("#000000")


class QrCodeView(Gtk.Widget):
    """Draws the modules as rectangles: a scaled bitmap would be blurred.

    Always black on white (also in dark mode), as scanners expect, on a
    square of the largest whole number of pixels per module that fits.
    """

    __gtype_name__ = "RetchatQrCodeView"

    def __init__(self, text: str = "", natural_size: int = 280):
        super().__init__()
        self._matrix: List[List[bool]] = []
        self._natural = natural_size
        self.add_css_class("qr-code")
        self.set_text(text)

    def set_text(self, text: str):
        self._matrix = qr_matrix(text) if text else []
        self.queue_draw()

    @property
    def modules(self) -> int:
        return len(self._matrix)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, _orientation, _for_size):
        minimum = self.modules * 2  # 2 px per module still scans
        return minimum, max(minimum, self._natural), -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot):
        n = self.modules
        if not n:
            return
        width, height = self.get_width(), self.get_height()
        cell = max(1, min(width, height) // n)
        side = cell * n
        x0, y0 = (width - side) // 2, (height - side) // 2
        snapshot.append_color(_WHITE, Graphene.Rect().init(x0, y0, side, side))
        for y, row in enumerate(self._matrix):
            x = 0
            while x < n:
                if row[x]:
                    start = x
                    while x < n and row[x]:
                        x += 1
                    # One rectangle per horizontal run of dark modules
                    snapshot.append_color(_BLACK, Graphene.Rect().init(
                        x0 + start * cell, y0 + y * cell, (x - start) * cell, cell))
                else:
                    x += 1
