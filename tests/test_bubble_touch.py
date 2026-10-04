"""Touches on message text are kept from the label (so long press and scrolling work), except on links."""

import time

import pytest


def _gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        return Gtk.init_check()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _gtk_available(), reason="needs a display")


class FakeEvent:
    """Enough of Gdk.Event for MessageBubble._on_label_touch."""

    def __init__(self, event_type, x=0.0, y=0.0):
        self._type, self._x, self._y = event_type, x, y

    def get_event_type(self):
        return self._type

    def get_position(self):
        return True, self._x, self._y


@pytest.fixture
def shown_bubble():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
    from retchat.models import MessageItem
    from retchat.widgets.message_bubble import MessageBubble

    text = "Schau mal nomadnetwork://" + "ab" * 16 + ":/page/index.mu dort"
    bubble = MessageBubble()
    bubble.bind(MessageItem({"message_hash": "cd" * 32, "content": text, "timestamp": 1.0}))
    window = Gtk.Window(child=bubble, default_width=420)
    window.present()
    ctx = GLib.MainContext.default()
    end = time.monotonic() + 3
    while time.monotonic() < end and bubble.label.get_width() == 0:
        ctx.iteration(False)
    for _ in range(20):
        ctx.iteration(False)
    yield bubble, text
    window.destroy()


def _surface_point(bubble, index):
    """Surface coordinates of the character at byte ``index`` of the label."""
    from gi.repository import Graphene, Pango
    label = bubble.label
    rect = label.get_layout().index_to_pos(index)
    ox, oy = label.get_layout_offsets()
    x = ox + (rect.x + rect.width / 2) / Pango.SCALE
    y = oy + (rect.y + rect.height / 2) / Pango.SCALE
    native = label.get_native()
    ok, point = label.compute_point(native, Graphene.Point().init(x, y))
    tx, ty = native.get_surface_transform()
    return point.x + tx, point.y + ty


def test_touch_on_text_is_kept_from_the_label(shown_bubble):
    from gi.repository import Gdk
    bubble, text = shown_bubble
    x, y = _surface_point(bubble, 2)  # "Schau"
    assert bubble._on_label_touch(None, FakeEvent(Gdk.EventType.TOUCH_BEGIN, x, y)) is True
    assert bubble._on_label_touch(None, FakeEvent(Gdk.EventType.TOUCH_UPDATE, x, y)) is True
    assert bubble._on_label_touch(None, FakeEvent(Gdk.EventType.TOUCH_END, x, y)) is True


def test_touch_on_link_reaches_the_label(shown_bubble):
    from gi.repository import Gdk
    bubble, text = shown_bubble
    x, y = _surface_point(bubble, text.index("nomadnetwork") + 5)
    assert bubble._on_label_touch(None, FakeEvent(Gdk.EventType.TOUCH_BEGIN, x, y)) is False
    assert bubble._on_label_touch(None, FakeEvent(Gdk.EventType.TOUCH_END, x, y)) is False
    # The next touch on plain text is kept away again
    x, y = _surface_point(bubble, len(text) - 2)  # "dort"
    assert bubble._on_label_touch(None, FakeEvent(Gdk.EventType.TOUCH_BEGIN, x, y)) is True


def test_mouse_reaches_the_label(shown_bubble):
    from gi.repository import Gdk
    bubble, _text = shown_bubble
    for event_type in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.MOTION_NOTIFY, Gdk.EventType.BUTTON_RELEASE):
        assert bubble._on_label_touch(None, FakeEvent(event_type, 5, 5)) is False


def test_link_ranges_use_byte_offsets():
    """Pango indexes bytes; text before a link may contain multi-byte characters."""
    import gi
    gi.require_version("Gtk", "4.0")
    from retchat.models import MessageItem
    from retchat.widgets.message_bubble import MessageBubble
    text = "Grüße 👋 nomadnetwork://" + "ab" * 16
    bubble = MessageBubble()
    bubble.bind(MessageItem({"message_hash": "cd" * 32, "content": text, "timestamp": 1.0}))
    start = text.index("nomadnetwork")
    assert bubble._link_ranges == [(len(text[:start].encode()), len(text.encode()))]
