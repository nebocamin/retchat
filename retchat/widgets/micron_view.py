"""GTK rendering of parsed Micron pages (see retchat.micron).

Pages are drawn with the colours of the Adwaita theme: text without an
explicit colour uses the theme's text colour, links the accent colour. The
page's own colours are kept where they are set, but adjusted if they would
be hard to read on the theme's (or their own) background. Pages are shown
in a monospace font, as they are written for terminals (ASCII art, tables).

Consecutive text lines with the same layout share one Gtk.Label (up to
MAX_GROUP_LINES), so long pages don't need a widget per line.
"""

import colorsys
import math
import time
from typing import Callable, Dict, List, Optional, Tuple

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Graphene', '1.0')
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk, Pango

from retchat.micron import SECTION_INDENT, Divider, Document, Field, Line, Link, Placeholder, Span

# Adwaita's window background and text colour (light / dark)
THEME_BG = {False: "#fafafb", True: "#222226"}
THEME_FG = {False: "#2e2e32", True: "#ffffff"}
MIN_CONTRAST = 4.5  # WCAG AA for normal text
MAX_FIELD_MIN_CHARS = 12  # minimum width of multi-line fields
# Lines per label: GTK re-measures a label as a whole, huge ones block the UI.
MAX_GROUP_LINES = 30
# Pages are written for terminals, typically 80-100 columns wide.
PAGE_MAX_WIDTH = 900
# Touch: widgets that handle touches themselves; momentum after a swipe
TOUCH_OWN_WIDGETS = (Gtk.Entry, Gtk.Text, Gtk.TextView, Gtk.CheckButton, Gtk.Scrollbar)
KINETIC_TIME_CONSTANT = 0.35  # seconds for the speed to drop to 37 %
KINETIC_MIN_SPEED = 30.0  # px/s
SWIPE_SAMPLE_WINDOW = 0.1  # seconds of movement used for the release speed


# --- Colours ---------------------------------------------------------------------------

def _rgb(color: str) -> Tuple[float, float, float]:
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def _hex(rgb: Tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def _luminance(color: str) -> float:
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_color(fg: str, bg: str, minimum: float = MIN_CONTRAST) -> str:
    """``fg``, made lighter or darker (same hue) until it is readable on ``bg``."""
    if contrast_ratio(fg, bg) >= minimum:
        return fg
    toward_white = contrast_ratio("#ffffff", bg) > contrast_ratio("#000000", bg)
    h, lightness, s = colorsys.rgb_to_hls(*_rgb(fg))
    for _ in range(25):
        lightness = min(1.0, lightness + 0.04) if toward_white else max(0.0, lightness - 0.04)
        candidate = _hex(colorsys.hls_to_rgb(h, lightness, s))
        if contrast_ratio(candidate, bg) >= minimum:
            return candidate
    return "#ffffff" if toward_white else "#000000"


# --- Renderer ---------------------------------------------------------------------------

def _is_art(text: str) -> bool:
    """Whether a line draws with box drawing, block or braille characters."""
    return any("\u2500" <= c <= "\u259f" or "\u2800" <= c <= "\u28ff" for c in text)


class _Rich:
    """Pango markup plus the character ranges of its links in the plain text."""

    def __init__(self, markup: str = "", length: int = 0, links: Optional[List[Tuple[int, int, int]]] = None):
        self.markup = markup
        self.length = length  # characters of plain text
        self.links = links or []  # (start, end, index into MicronView._links)

    @staticmethod
    def join_lines(parts: List["_Rich"]) -> "_Rich":
        joined = _Rich()
        for i, part in enumerate(parts):
            if i:
                joined.markup += "\n"
                joined.length += 1
            joined.links += [(a + joined.length, b + joined.length, idx) for a, b, idx in part.links]
            joined.markup += part.markup
            joined.length += part.length
        return joined


class _Row(GObject.Object):
    """One row of the page: a widget, built when it first becomes visible."""

    __gtype_name__ = "RetchatMicronRow"

    def __init__(self, build: Callable[[], Gtk.Widget], widget: Optional[Gtk.Widget] = None):
        super().__init__()
        self._build = build
        self.widget = widget

    def get_widget(self) -> Gtk.Widget:
        if self.widget is None:
            self.widget = self._build()
        return self.widget


class MicronView(Adw.Bin):
    """Shows a micron Document; ``on_link(link)`` is called for activated links.

    The page is a Gtk.ListView of row widgets (groups of lines, headings,
    form lines, ...), so only the visible part is laid out: pages can have
    thousands of lines. The markup is prepared up front; text widgets are
    created when their row is first shown and then kept. Form lines are
    created right away, so a submit link finds all fields.
    """

    def __init__(self, on_link: Callable[[Link], None]):
        super().__init__(hexpand=True, vexpand=True)
        self._on_link = on_link
        self._links: List[Link] = []
        self._anchors: Dict[str, int] = {}  # anchor name -> row index
        # Form widgets by field name: (kind, widget, value)
        self._fields: Dict[str, List[Tuple[str, Gtk.Widget, str]]] = {}
        self._dark = False
        self._char_width = 8.0
        # Labels with links: their link ranges, for taps (see _touch_tap)
        self._label_links: Dict[Gtk.Label, List[Tuple[int, int, int]]] = {}
        self._touch: Optional[dict] = None
        self._kinetic_tick = 0

        self.store = Gio.ListStore(item_type=_Row)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", lambda _f, li: li.set_child(li.get_item().get_widget()))
        factory.connect("unbind", lambda _f, li: li.set_child(None))
        self.list_view = Gtk.ListView(model=Gtk.NoSelection(model=self.store), factory=factory)
        self.list_view.add_css_class("micron-page")
        self.scrolled = Gtk.ScrolledWindow(
            child=Adw.ClampScrollable(child=self.list_view, maximum_size=PAGE_MAX_WIDTH,
                                      tightening_threshold=PAGE_MAX_WIDTH - 120),
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.set_child(self.scrolled)

        # The page itself holds the keyboard focus and scrolls with the keys.
        # Given to the list view, the focus would go to the first label, which
        # then shows a text cursor (or selects all its text).
        self.set_focusable(True)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

        # Touch scrolling and link taps (see _on_touch_begin)
        touch = Gtk.GestureDrag(touch_only=True, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        touch.connect("drag-begin", self._on_touch_begin)
        touch.connect("drag-update", self._on_touch_update)
        touch.connect("drag-end", self._on_touch_end)
        touch.connect("cancel", self._on_touch_cancel)
        self.add_controller(touch)
        # Any other input (click, wheel, key) stops the momentum of a swipe.
        stopper = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        stopper.connect("event", self._on_any_input)
        self.add_controller(stopper)

        # Row backgrounds need CSS (a label's markup only colours the text).
        self._css = Gtk.CssProvider()
        self._css_classes: Dict[str, str] = {}
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(display, self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    @staticmethod
    def _on_setup(_factory, list_item: Gtk.ListItem):
        list_item.set_activatable(False)
        list_item.set_selectable(False)
        list_item.set_focusable(False)

    # --- Public API ---------------------------------------------------------------------

    def render(self, doc: Document, dark: bool):
        self.clear()
        self._dark = dark
        self._char_width = self._measure_char_width()

        rows: List[_Row] = []
        row_of_block: Dict[int, int] = {}
        group: List[Line] = []
        group_key = None
        group_start = 0

        def add(row: _Row, first_block: int, count: int = 1):
            for idx in range(first_block, first_block + count):
                row_of_block[idx] = len(rows)
            rows.append(row)

        def flush():
            nonlocal group, group_key
            if group:
                lines = group
                if group_key[0] == "text":
                    markup = _Rich.join_lines([self._markup(line.parts, line.row_bg) for line in lines])
                    bg_class = self._bg_class(lines[0].row_bg) if lines[0].row_bg else None
                    add(_Row(lambda m=markup, first=lines[0], c=bg_class: self._text_widget(m, first, c)),
                        group_start, len(lines))
                else:
                    markup = _Rich.join_lines([self._markup(line.parts, line.row_bg) for line in lines])
                    bg_class = self._bg_class(lines[0].row_bg) if lines[0].row_bg else None
                    add(_Row(lambda m=markup, first=lines[0], c=bg_class: self._literal_widget(m, first, c)),
                        group_start, len(lines))
            group, group_key = [], None

        for index, block in enumerate(doc.blocks):
            if isinstance(block, Line) and not block.heading and not any(isinstance(p, Field) for p in block.parts):
                if block.literal or (_is_art(block.text) and not any(
                        isinstance(p, Span) and p.link for p in block.parts)):
                    # Literal blocks and block/box/braille art: no wrapping (it
                    # would break the picture), scrolled sideways if too wide.
                    key = ("literal", block.align, block.depth, block.row_bg)
                elif not block.parts and not block.row_bg and group_key and group_key[0] == "text":
                    key = group_key  # an empty line continues the paragraph
                else:
                    key = ("text", block.align, block.depth, block.row_bg)
                if key != group_key or block.anchors or len(group) >= MAX_GROUP_LINES:
                    flush()
                    group_key, group_start = key, index
                group.append(block)
                continue

            flush()
            if isinstance(block, Line) and block.heading:
                markup = self._markup(block.parts)
                add(_Row(lambda m=markup, b=block: self._heading_widget(m, b)), index)
            elif isinstance(block, Line):
                add(_Row(None, self._form_line(block)), index)  # eager: fields must exist for submitting
            elif isinstance(block, Divider):
                add(_Row(lambda b=block: self._divider(b)), index)
            else:
                add(_Row(lambda b=block: self._placeholder(b)), index)
        flush()

        self._anchors = {name: row_of_block[i] for name, i in doc.anchors.items() if i in row_of_block}
        self._css.load_from_string("".join(
            f".{cls} {{ background-color: {color}; }}\n" for color, cls in self._css_classes.items()))
        self.store.splice(0, 0, rows)

    def clear(self):
        self._stop_kinetic()
        self._touch = None
        # Move the focus off the page first: removing a focused (selectable or
        # link) label while the view is being hidden leaves GTK with a stale
        # focus widget (Gtk-CRITICAL in gtk_widget_is_ancestor while drawing).
        # (Not onto the list view: it would pass it on to a row that is removed.)
        root = self.get_root()
        focus = root.get_focus() if root is not None else None
        if focus is not None and (focus is self.list_view or focus.is_ancestor(self.list_view)):
            if not self.grab_focus():
                root.set_focus(None)
        self.store.remove_all()
        self._links = []
        self._anchors = {}
        self._fields = {}
        self._label_links = {}
        self._css_classes = {}

    def take_focus(self):
        """Focus the page (not its first label) and drop a selection made by
        a label that received the focus before (labels select all then)."""
        self.grab_focus()
        for i in range(self.store.get_n_items()):
            widget = self.store.get_item(i).widget
            stack = [widget] if widget is not None else []
            while stack:
                w = stack.pop()
                if isinstance(w, Gtk.Label):
                    if w.get_selectable() and w.get_selection_bounds()[0]:
                        w.select_region(0, 0)
                    continue
                child = w.get_first_child()
                while child is not None:
                    stack.append(child)
                    child = child.get_next_sibling()

    def _on_key_pressed(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        adj = self.scrolled.get_vadjustment()
        page = adj.get_page_size() * 0.9
        step = {Gdk.KEY_Up: -48, Gdk.KEY_Down: 48, Gdk.KEY_Page_Up: -page, Gdk.KEY_Page_Down: page,
                Gdk.KEY_space: page, Gdk.KEY_BackSpace: -page}.get(keyval)
        if keyval == Gdk.KEY_Home:
            adj.set_value(adj.get_lower())
        elif keyval == Gdk.KEY_End:
            adj.set_value(adj.get_upper())
        elif step is not None:
            adj.set_value(adj.get_value() + step)
        else:
            return False
        return True

    # --- Touch --------------------------------------------------------------------------
    #
    # A label claims a touch as soon as it is pressed (for link clicks and text
    # selection); GTK then denies the scrolled window's own touch scrolling,
    # so dragging on text would select it. The page therefore takes touches on
    # its text first (capture phase): dragging scrolls, with momentum after a
    # swipe, sideways too inside wide art blocks; a tap opens the link under
    # the finger. Form fields and scrollbars get their touches as usual. The
    # mouse is not affected (text stays selectable).

    def _on_touch_begin(self, gesture: Gtk.GestureDrag, x: float, y: float):
        self._stop_kinetic()
        target = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        hscroller = None
        widget = target
        while widget is not None and widget is not self:
            if isinstance(widget, TOUCH_OWN_WIDGETS):
                self._touch = None
                gesture.set_state(Gtk.EventSequenceState.DENIED)
                return
            if isinstance(widget, Gtk.ScrolledWindow) and widget is not self.scrolled and hscroller is None:
                hscroller = widget
            widget = widget.get_parent()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        hadj = hscroller.get_hadjustment() if hscroller is not None else None
        self._touch = {
            "target": target, "x": x, "y": y, "moved": False,
            "v0": self.scrolled.get_vadjustment().get_value(),
            "hadj": hadj, "h0": hadj.get_value() if hadj is not None else 0.0,
            "samples": [(time.monotonic(), 0.0, 0.0)],
        }

    def _on_touch_update(self, _gesture, dx: float, dy: float):
        touch = self._touch
        if touch is None:
            return
        if not touch["moved"]:
            threshold = Gtk.Settings.get_default().get_property("gtk-dnd-drag-threshold")
            if math.hypot(dx, dy) < threshold:
                return
            touch["moved"] = True
        self.scrolled.get_vadjustment().set_value(touch["v0"] - dy)  # clamped by the adjustment
        if touch["hadj"] is not None:
            touch["hadj"].set_value(touch["h0"] - dx)
        touch["samples"] = touch["samples"][-10:] + [(time.monotonic(), dx, dy)]

    def _on_touch_end(self, _gesture, dx: float, dy: float):
        touch, self._touch = self._touch, None
        if touch is None:
            return
        if not touch["moved"]:
            self._touch_tap(touch["target"], touch["x"], touch["y"])
            return
        # Speed at release, from the movement of the last moment; none if the
        # finger rested before it was lifted.
        now = time.monotonic()
        recent = [s for s in touch["samples"] if now - s[0] <= SWIPE_SAMPLE_WINDOW]
        if len(recent) >= 2 and now - recent[0][0] > 0.005:
            span = now - recent[0][0]
            speed_x = -(dx - recent[0][1]) / span if touch["hadj"] is not None else 0.0
            self._start_kinetic(speed_x, -(dy - recent[0][2]) / span, touch["hadj"])

    def _on_touch_cancel(self, _gesture, _sequence):
        self._touch = None

    def _touch_tap(self, target: Optional[Gtk.Widget], x: float, y: float):
        """Open the link at (x, y) (page coordinates) of the tapped label, if any."""
        link = self.link_at(target, x, y)
        if link is not None:
            self._on_link(link)

    def link_at(self, label: Optional[Gtk.Widget], x: float, y: float) -> Optional[Link]:
        if not isinstance(label, Gtk.Label):
            return None
        spans = self._label_links.get(label)
        if not spans:
            return None
        ok, point = self.compute_point(label, Graphene.Point().init(x, y))
        if not ok:
            return None
        off_x, off_y = label.get_layout_offsets()
        inside, index, _trailing = label.get_layout().xy_to_index(
            round((point.x - off_x) * Pango.SCALE), round((point.y - off_y) * Pango.SCALE))
        if not inside:
            return None
        char = len(label.get_text().encode("utf-8")[:index].decode("utf-8", errors="ignore"))
        for start, end, link_index in spans:
            if start <= char < end:
                return self._links[link_index]
        return None

    def _start_kinetic(self, speed_x: float, speed_y: float, hadj: Optional[Gtk.Adjustment]):
        if abs(speed_x) < KINETIC_MIN_SPEED and abs(speed_y) < KINETIC_MIN_SPEED:
            return
        self._kinetic = {"x": speed_x, "y": speed_y, "hadj": hadj, "last": None}
        self._kinetic_tick = self.add_tick_callback(self._on_kinetic_tick)

    def _on_kinetic_tick(self, _widget, clock: Gdk.FrameClock) -> bool:
        state = self._kinetic
        now = clock.get_frame_time() / 1_000_000
        if state["last"] is None:
            state["last"] = now
            return GLib.SOURCE_CONTINUE
        dt, state["last"] = now - state["last"], now
        decay = math.exp(-dt / KINETIC_TIME_CONSTANT)
        moving = False
        for key, adj in (("y", self.scrolled.get_vadjustment()), ("x", state["hadj"])):
            speed = state[key]
            if adj is None or abs(speed) < KINETIC_MIN_SPEED:
                state[key] = 0.0
                continue
            before = adj.get_value()
            adj.set_value(before + speed * dt)
            if adj.get_value() == before:  # reached the end
                state[key] = 0.0
            else:
                state[key] = speed * decay
                moving = True
        if not moving:
            self._kinetic_tick = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _on_any_input(self, _controller, event: Optional[Gdk.Event]) -> bool:
        # (PyGObject passes None for some event types it can't wrap)
        if self._kinetic_tick and event is not None and event.get_event_type() in (
                Gdk.EventType.BUTTON_PRESS, Gdk.EventType.SCROLL, Gdk.EventType.KEY_PRESS):
            self._stop_kinetic()
        return False  # never consumes the event

    def _stop_kinetic(self):
        if self._kinetic_tick:
            self.remove_tick_callback(self._kinetic_tick)
            self._kinetic_tick = 0

    def has_anchor(self, name: str) -> bool:
        return name in self._anchors

    def scroll_to_anchor(self, name: Optional[str]):
        """Show the row of an anchor at the top (the page start without name)."""
        index = self._anchors.get(name, 0) if name else 0
        n_items = self.store.get_n_items()
        if not n_items:
            return
        if index == 0:
            self.scrolled.get_vadjustment().set_value(0)
            return
        # scroll_to() only makes a row visible; coming from below puts it at the top.
        self.list_view.scroll_to(n_items - 1, Gtk.ListScrollFlags.NONE, None)
        GLib.idle_add(lambda: self.list_view.scroll_to(index, Gtk.ListScrollFlags.NONE, None) and False)

    def form_data(self, spec: List[str]) -> Dict[str, str]:
        """``field_*`` values of the fields a link submits ("*" = all)."""
        submit_all = "*" in spec
        names = {s for s in spec if "=" not in s and s != "*"}
        data: Dict[str, str] = {}
        for name, entries in self._fields.items():
            if not (submit_all or name in names):
                continue
            key = "field_" + name
            for kind, widget, value in entries:
                if kind == "field":
                    if isinstance(widget, Gtk.TextView):
                        buf = widget.get_buffer()
                        data[key] = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
                    else:
                        data[key] = widget.get_text()
                elif widget.get_active():
                    if kind == "checkbox" and data.get(key):
                        data[key] += "," + value
                    else:
                        data[key] = value
        return data

    # --- Row widgets --------------------------------------------------------------------

    def _indent(self, depth: int) -> int:
        return round(max(0, depth - 1) * SECTION_INDENT * self._char_width)

    def _text_widget(self, markup: "_Rich", first: Line, bg_class: Optional[str]) -> Gtk.Widget:
        label = self._label(markup, first.align)
        label.set_margin_start(self._indent(first.depth))
        label.set_margin_end(self._indent(first.depth))
        if bg_class:
            label.add_css_class(bg_class)
        return label

    def _literal_widget(self, markup: "_Rich", first: Line, bg_class: Optional[str]) -> Gtk.Widget:
        label = self._label(markup, first.align, wrap=False)
        # As wide as the content (so it can be centred), scrolled if too wide
        scroller = Gtk.ScrolledWindow(child=label, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                      hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                      propagate_natural_height=True, propagate_natural_width=True,
                                      halign={"center": Gtk.Align.CENTER, "right": Gtk.Align.END}.get(
                                          first.align, Gtk.Align.FILL))
        scroller.set_margin_start(self._indent(first.depth))
        if bg_class:
            scroller.add_css_class(bg_class)
        return scroller

    def _heading_widget(self, markup: "_Rich", line: Line) -> Gtk.Widget:
        box = Gtk.Box()
        box.add_css_class("micron-heading")
        box.add_css_class(f"micron-h{min(line.heading, 3)}")
        label = self._label(markup, line.align)
        label.set_margin_start(self._indent(line.depth))
        label.set_hexpand(True)
        box.append(label)
        return box

    def _form_line(self, line: Line) -> Gtk.Widget:
        box = Gtk.Box(spacing=0, halign={"center": Gtk.Align.CENTER, "right": Gtk.Align.END}.get(line.align, Gtk.Align.FILL))
        box.set_margin_start(self._indent(line.depth))
        box.add_css_class("micron-form-line")
        for part in line.parts:
            if isinstance(part, Span):
                label = self._label(self._markup([part]), "left")
                label.set_hexpand(False)
                label.set_valign(Gtk.Align.CENTER)
                label.set_wrap_mode(Pango.WrapMode.WORD)  # never split words next to a field
                box.append(label)
            else:
                box.append(self._field_widget(part))
        return box

    def _field_widget(self, fld: Field) -> Gtk.Widget:
        entries = self._fields.setdefault(fld.name, [])
        if fld.kind == "field" and fld.rows > 1:
            view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False)
            view.get_buffer().set_text(fld.value)
            view.add_css_class("micron-field")
            # Small minimum width (the page must not force the window wider than
            # a phone), otherwise it takes the rest of the line.
            frame = Gtk.ScrolledWindow(child=view, hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True)
            frame.set_size_request(round(min(fld.width, MAX_FIELD_MIN_CHARS) * self._char_width) + 12,
                                   round(fld.rows * self._char_width * 2.2) + 8)
            frame.add_css_class("micron-field-frame")
            entries.append(("field", view, ""))
            return frame
        if fld.kind == "field":
            # width_chars is the minimum, max_width_chars the natural width
            entry = Gtk.Entry(text=fld.value, visibility=not fld.masked, valign=Gtk.Align.CENTER,
                              width_chars=min(fld.width, 10), max_width_chars=min(fld.width, 40))
            entry.add_css_class("micron-field")
            entries.append(("field", entry, ""))
            return entry
        button = Gtk.CheckButton(label=fld.label, active=fld.checked, valign=Gtk.Align.CENTER)
        if fld.kind == "radio":
            leader = next((w for kind, w, _v in entries if kind == "radio"), None)
            if leader is not None:
                button.set_group(leader)
        entries.append((fld.kind, button, fld.value))
        return button

    def _divider(self, divider: Divider) -> Gtk.Widget:
        if divider.char in ("\u2500", "-", "\u2501"):
            widget: Gtk.Widget = Gtk.Separator(margin_top=9, margin_bottom=9)
        else:
            # A line of the character; clipped, so it never widens the page.
            label = Gtk.Label(label=divider.char * 400, xalign=0.0)
            label.add_css_class("micron-divider")
            widget = Gtk.ScrolledWindow(child=label, hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                        vscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                        can_target=False)
        widget.set_margin_start(self._indent(divider.depth))
        widget.set_margin_end(self._indent(divider.depth))
        return widget

    def _placeholder(self, block: Placeholder) -> Gtk.Widget:
        text = f"[Bild: {block.text}]" if block.kind == "image" else f"[{block.text}]"
        label = Gtk.Label(label=text, xalign=0.0, wrap=True)
        label.add_css_class("dim-label")
        label.set_margin_start(self._indent(block.depth))
        return label

    # --- Helpers ------------------------------------------------------------------------

    def _label(self, rich: "_Rich", align: str, wrap: bool = True) -> Gtk.Label:
        xalign = {"center": 0.5, "right": 1.0}.get(align, 0.0)
        label = Gtk.Label(use_markup=True, xalign=xalign, selectable=True, wrap=wrap,
                          wrap_mode=Pango.WrapMode.WORD_CHAR,
                          justify={"center": Gtk.Justification.CENTER,
                                   "right": Gtk.Justification.RIGHT}.get(align, Gtk.Justification.LEFT))
        label.set_markup(rich.markup or " ")
        label.connect("activate-link", self._on_activate_link)
        if rich.links:
            self._label_links[label] = rich.links
        return label

    def _markup(self, parts, row_bg: Optional[str] = None) -> "_Rich":
        out = []
        length = 0
        links = []
        theme_bg, theme_fg = THEME_BG[self._dark], THEME_FG[self._dark]
        for part in parts:
            if not isinstance(part, Span):
                continue
            style = part.style
            background = style.bg or row_bg
            fg = style.fg
            if fg is None and background:
                fg = theme_fg  # the theme colour may be unreadable on the page's background
            attrs = []
            if fg:
                attrs.append(f'foreground="{readable_color(fg, background or theme_bg)}"')
            if style.bg:
                attrs.append(f'background="{style.bg}"')
            if style.bold:
                attrs.append('weight="bold"')
            if style.italic:
                attrs.append('style="italic"')
            if style.underline or (part.link and fg):
                attrs.append('underline="single"')
            text = GLib.markup_escape_text(part.text)
            if attrs:
                text = f"<span {' '.join(attrs)}>{text}</span>"
            if part.link is not None:
                self._links.append(part.link)
                links.append((length, length + len(part.text), len(self._links) - 1))
                title = GLib.markup_escape_text(part.link.url, -1).replace('"', "&quot;")
                text = f'<a href="{len(self._links) - 1}" title="{title}">{text}</a>'
            out.append(text)
            length += len(part.text)
        return _Rich("".join(out), length, links)

    def _bg_class(self, color: str) -> str:
        cls = self._css_classes.get(color)
        if cls is None:
            cls = f"micron-bg-{color[1:]}"
            self._css_classes[color] = cls
        return cls

    def _measure_char_width(self) -> float:
        context = self.get_pango_context()
        font = Pango.FontDescription.from_string("Monospace")
        base = context.get_font_description()
        if base is not None and base.get_size() > 0:
            font.set_size(base.get_size())
        metrics = context.get_metrics(font, None)
        width = metrics.get_approximate_char_width() / Pango.SCALE
        return width if width > 0 else 8.0

    def _on_activate_link(self, _label: Gtk.Label, uri: str) -> bool:
        try:
            link = self._links[int(uri)]
        except (ValueError, IndexError):
            return True
        self._on_link(link)
        return True  # handled; GTK would otherwise try to open the "URI"
