#!/usr/bin/env python3
"""Render README screenshots of Retchat with dummy data.

Runs the real RetchatWindow against a fake service, headless via GTK's
Broadway backend (needs `gtk4-broadwayd`), and saves framed PNGs next to
this script:

    .venv/bin/python data/screenshots/generate_screenshots.py
"""
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BROADWAY_DISPLAY = ":42"

if os.environ.get("GDK_BACKEND") != "broadway":
    if not shutil.which("gtk4-broadwayd"):
        sys.exit("gtk4-broadwayd not found")
    daemon = subprocess.Popen(["gtk4-broadwayd", BROADWAY_DISPLAY],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    env = dict(os.environ, GDK_BACKEND="broadway", BROADWAY_DISPLAY=BROADWAY_DISPLAY, LANG="de_DE.UTF-8")
    try:
        sys.exit(subprocess.call([sys.executable, *sys.argv], env=env))
    finally:
        daemon.terminate()

sys.path.insert(0, ROOT)

import cairo  # noqa: E402
import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

from retchat import app as retchat_app  # noqa: E402
from retchat.window import RetchatWindow  # noqa: E402

NOW = time.time()
TODAY = time.mktime(time.localtime(NOW)[:3] + (0, 0, 0, 0, 0, -1))


def at(hour, minute, days_ago=0):
    return TODAY - days_ago * 86400 + hour * 3600 + minute * 60


def h(seed):
    """Deterministic fake 16-byte hash."""
    return "".join(f"{(seed * 2654435761 + i * 40503) % 256:02x}" for i in range(16))


# --------------------------------------------------------------------------- dummy data

PHOTO = os.path.join("/tmp", "retchat-screenshot-photo.png")


def make_photo(path):
    """A small sunset landscape with a radio mast, used as image message."""
    w, hgt = 640, 420
    s = cairo.ImageSurface(cairo.FORMAT_RGB24, w, hgt)
    c = cairo.Context(s)
    sky = cairo.LinearGradient(0, 0, 0, hgt)
    sky.add_color_stop_rgb(0, 0.36, 0.33, 0.62)
    sky.add_color_stop_rgb(0.55, 0.96, 0.52, 0.47)
    sky.add_color_stop_rgb(1, 0.99, 0.80, 0.52)
    c.set_source(sky)
    c.paint()
    c.set_source_rgba(1, 0.93, 0.70, 0.95)
    c.arc(430, 270, 46, 0, 2 * math.pi)
    c.fill()
    for color, base, amp, freq, phase in ((((0.45, 0.25, 0.42), 300, 38, 0.012, 0.6)),
                                          (((0.30, 0.16, 0.32), 340, 30, 0.017, 2.1)),
                                          (((0.18, 0.10, 0.22), 380, 22, 0.023, 4.0))):
        c.set_source_rgb(*color)
        c.move_to(0, hgt)
        for x in range(0, w + 1, 8):
            c.line_to(x, base - amp * math.sin(x * freq + phase))
        c.line_to(w, hgt)
        c.close_path()
        c.fill()
    # radio mast with antenna on the hill
    c.set_source_rgb(0.12, 0.07, 0.15)
    c.set_line_width(4)
    c.move_to(170, 330)
    c.line_to(170, 205)
    c.stroke()
    c.set_line_width(2.5)
    for dy in (0, 22, 44):
        c.move_to(155, 225 + dy)
        c.line_to(185, 225 + dy)
        c.stroke()
    c.set_source_rgba(1, 1, 1, 0.9)
    for r in (14, 26, 38):
        c.arc(170, 205, r, -2.3, -0.84)
        c.stroke()
    s.write_to_png(path)


ALICE, BOB, CAROL, DAVE, MESHCAFE, ERIN = (h(i) for i in range(1, 7))

CONTACTS = [
    dict(destination_hash=ALICE, display_name="Alice", hops=2, unread_count=0),
    dict(destination_hash=BOB, display_name="Bob", hops=1, unread_count=2),
    dict(destination_hash=CAROL, display_name="Carol (LoRa)", hops=4, unread_count=0),
    dict(destination_hash=MESHCAFE, display_name="Mesh Café", hops=3, unread_count=0),
    dict(destination_hash=DAVE, display_name="Dave", hops=1, unread_count=0),
    dict(destination_hash=ERIN, display_name="Erin", hops=5, unread_count=0),
]


DATASHEET = os.path.join("/tmp", "retchat-screenshot-datasheet")


def make_datasheet(path):
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n" + b"0" * 184_000)


def msg(conv, n, text, ts, out=False, state=2, hops=0, image=None, files=None):
    return {
        "message_hash": f"{conv}{n:04d}",
        "conversation_hash": conv,
        "is_outgoing": out,
        "content": text,
        "timestamp": ts,
        "state": state,
        "hops": hops,
        "image_path": image,
        "image_name": "sunset.png" if image else None,
        "image_size": os.path.getsize(image) if image else None,
        "files": files or [],
    }


def build_messages():
    a = ALICE
    return {
        ALICE: [
            msg(a, 1, "Hey! Did your new RNode arrive?", at(18, 2), hops=2),
            msg(a, 2, "Yes, flashed it this morning. It's on the balcony now.", at(18, 5), out=True),
            msg(a, 3, "Nice. Which antenna are you using?", at(18, 6), hops=2),
            msg(a, 4, "A 5 dBi collinear for 868 MHz. I get the Reticulum community node over "
                      "four hops, no internet involved at all.", at(18, 9), out=True),
            msg(a, 5, "Here's the view from my mast tonight", at(18, 21), hops=2, image=PHOTO),
            msg(a, 6, "Wow, that looks amazing!", at(18, 23), out=True),
            msg(a, 9, "Datasheet of the antenna, in case you want one too", at(18, 23), hops=2,
                files=[{"path": DATASHEET, "name": "collinear-868-datasheet.pdf",
                        "size": os.path.getsize(DATASHEET)}]),
            msg(a, 7, "Want to meet at the hill on Saturday and test the range?", at(18, 24), hops=2),
            msg(a, 8, "Absolutely, I'll bring the portable node!", at(18, 26), out=True, state=0),
        ],
        BOB: [msg(BOB, 1, "Are you coming to the meetup?", at(17, 40), hops=1),
              msg(BOB, 2, "I can bring spare antennas", at(17, 41), hops=1)],
        CAROL: [msg(CAROL, 1, "Got your message via the propagation node 👍", at(21, 12, 1), hops=4)],
        MESHCAFE: [msg(MESHCAFE, 1, "Open mesh night every Thursday at 7 pm", at(10, 0, 3), hops=3)],
        DAVE: [msg(DAVE, 1, "Thanks for the config!", at(9, 30, 6), out=True)],
        ERIN: [msg(ERIN, 1, "Signal report: -97 dBm, SNR 8", at(20, 5, 12), hops=5)],
    }


ANNOUNCES = [
    dict(destination_hash=h(40), display_name="Frieda", hops=2, receiving_interface="RNodeInterface[LoRa 868]"),
    dict(destination_hash=h(41), display_name="Hackerspace Node", hops=1, receiving_interface="TCPInterface[Hub]"),
    dict(destination_hash=h(42), display_name="Greg", hops=3, receiving_interface="AutoInterface[Local]"),
    dict(destination_hash=h(43), display_name="Mountain Relay", hops=6, receiving_interface="RNodeInterface[LoRa 868]"),
]


class DemoService:
    """Just enough of ReticulumService for the window."""

    app = None
    delivery_destination_hex = h(99)
    identity_hex = h(98)

    def __init__(self):
        self.messages = build_messages()

    def __getattr__(self, name):
        return lambda *a, **k: None

    def get_conversations(self, query=None):
        out = []
        for c in CONTACTS:
            last = self.messages[c["destination_hash"]][-1]
            text = last["content"] or ""
            if last["image_path"]:
                text = f"📷 {text}"
            out.append(dict(c, custom_name=None, last_message_text=text, last_message_time=last["timestamp"]))
        return out

    def get_conversation(self, dest):
        return next((c for c in self.get_conversations() if c["destination_hash"] == dest), None)

    def get_messages(self, dest):
        return list(self.messages.get(dest, []))

    def get_announces(self, query=None):
        return ANNOUNCES


# --------------------------------------------------------------------------- rendering helpers

def pump(ms):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.003)


def snapshot(win):
    w, hgt = win.get_width(), win.get_height()
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(win).snapshot(snap, w, hgt)
    tex = win.get_renderer().render_texture(snap.to_node(), Graphene.Rect().init(0, 0, w, hgt))
    tmp = "/tmp/retchat-screenshot-raw.png"
    tex.save_to_png(tmp)
    return cairo.ImageSurface.create_from_png(tmp)


def rounded(c, x, y, w, hgt, r):
    c.new_sub_path()
    c.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    c.arc(x + w - r, y + hgt - r, r, 0, math.pi / 2)
    c.arc(x + r, y + hgt - r, r, math.pi / 2, math.pi)
    c.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    c.close_path()


def framed(surfaces, gap=48, margin=40, radius=14):
    """Windows side by side with rounded corners and a soft shadow, transparent background."""
    width = sum(s.get_width() for s in surfaces) + gap * (len(surfaces) - 1) + 2 * margin
    height = max(s.get_height() for s in surfaces) + 2 * margin
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    c = cairo.Context(out)
    x = margin
    for s in surfaces:
        w, hgt = s.get_width(), s.get_height()
        y = margin + (height - 2 * margin - hgt) // 2
        spread = 28
        for i in range(spread, 0, -1):  # soft shadow, fading out quadratically
            alpha = 0.011 * ((spread - i + 1) / spread) ** 2
            c.set_source_rgba(0, 0, 0, alpha)
            rounded(c, x - i, y - i + 8, w + 2 * i, hgt + 2 * i, radius + i)
            c.fill()
        c.save()
        rounded(c, x, y, w, hgt, radius)
        c.clip()
        c.set_source_surface(s, x, y)
        c.paint()
        c.restore()
        c.set_source_rgba(0, 0, 0, 0.12)  # thin outline like GNOME windows
        c.set_line_width(1)
        rounded(c, x + 0.5, y + 0.5, w - 1, hgt - 1, radius)
        c.stroke()
        x += w + gap
    return out


# --------------------------------------------------------------------------- shots

class ShotApp(retchat_app.RetchatApp):
    def do_startup(self):
        Adw.Application.do_startup(self)
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-font-name", "Adwaita Sans 11")
        settings.set_property("gtk-decoration-layout", ":close")
        self._load_css()
        self._setup_icons()

    def do_activate(self):
        # Hold before returning: run_shots() starts from idle, and without a
        # window or hold the application would shut down right away.
        self.hold()
        GLib.idle_add(self.run_shots)

    def new_window(self, width, height, dark=False):
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
        win = RetchatWindow(self, db=None, service=DemoService())
        win.set_default_size(width, height)
        win.present()
        pump(1500)
        return win

    def select_chat(self, win, dest):
        win.conv_list_box.select_row(win.conv_rows[dest])
        pump(5000)  # also lets the overlay scroll indicator fade out

    def show_discover(self, win):
        win.sidebar_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        win.sidebar_stack.set_visible_child_name("discover")
        pump(1500)

    def save(self, name, *surfaces):
        path = os.path.join(HERE, name)
        framed(list(surfaces)).write_to_png(path)
        print("saved", os.path.relpath(path, ROOT))

    def run_shots(self):
        make_photo(PHOTO)
        make_datasheet(DATASHEET)

        win = self.new_window(1040, 700)
        self.select_chat(win, ALICE)
        self.save("desktop-chat.png", snapshot(win))
        win.destroy()

        win = self.new_window(1040, 700, dark=True)
        self.select_chat(win, ALICE)
        self.show_discover(win)
        self.save("desktop-dark.png", snapshot(win))
        win.destroy()

        phone_list = self.new_window(360, 740)
        pump(1000)
        list_shot = snapshot(phone_list)
        self.select_chat(phone_list, ALICE)
        chat_shot = snapshot(phone_list)
        phone_list.destroy()

        phone_disc = self.new_window(360, 740, dark=True)
        phone_disc.sidebar_stack.set_transition_type(Gtk.StackTransitionType.NONE)
        phone_disc.sidebar_stack.set_visible_child_name("discover")
        pump(2000)
        discover_shot = snapshot(phone_disc)
        phone_disc.destroy()
        self.save("mobile.png", list_shot, chat_shot, discover_shot)

        self.release()
        self.quit()
        return False


if __name__ == "__main__":
    app = ShotApp()
    app.set_application_id("org.selfmade.Retchat.Screenshots")  # not the id of a running Retchat
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    app.run([])
