"""Scrollable message history shared by the direct chat and relay chat views."""

from typing import Callable, Sequence

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, GObject, Gtk

CONTENT_MAX_WIDTH = 720
# Distance from the end (px) within which the view still counts as "at the bottom".
STICKY_THRESHOLD = 48.0


class ChatHistory(Adw.Bin):
    """A Gtk.ListView of messages that keeps the newest message in view.

    Rows are created by ``create_row`` and must provide ``bind(item)`` and
    ``unbind()``; they are recycled by the list view.

    Keeping the newest message visible:
    ``_stick_to_bottom`` is True while the end of the list is visible; it is
    derived from the scroll position (value-changed). While sticky, every
    change of the content height (new or re-measured rows) or viewport height
    (resize, on-screen keyboard) re-anchors the view to the last item with
    ``Gtk.ListView.scroll_to()``.

    Why re-anchoring is needed at all: a freshly appended row has not been
    measured when scroll_to() is first called, so GTK considers it visible
    already. Why it is deferred to idle: notify::upper is emitted during size
    allocation, and scroll_to() calls made from there are not applied.
    While a re-anchor is pending, intermediate scroll positions are ignored,
    otherwise a new row arriving in the same frame would clear the sticky state.
    """

    def __init__(self, item_type: GObject.GType, create_row: Callable[[], Gtk.Widget]):
        super().__init__(hexpand=True, vexpand=True)
        self._create_row = create_row
        self._stick_to_bottom = True
        self._rescroll_source = 0

        self.store = Gio.ListStore(item_type=item_type)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_setup)
        factory.connect("bind", lambda _f, li: li.get_child().bind(li.get_item()))
        factory.connect("unbind", lambda _f, li: li.get_child().unbind())

        self.list_view = Gtk.ListView(model=Gtk.NoSelection(model=self.store), factory=factory)
        self.list_view.add_css_class("chat-history")

        self.scrolled_window = Gtk.ScrolledWindow(
            child=Adw.ClampScrollable(
                child=self.list_view,
                maximum_size=CONTENT_MAX_WIDTH,
                tightening_threshold=CONTENT_MAX_WIDTH - 120,
            ),
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vexpand=True,
        )
        vadj = self.scrolled_window.get_vadjustment()
        vadj.connect("value-changed", self._on_scrolled)
        vadj.connect("notify::upper", self._on_extent_changed)
        vadj.connect("notify::page-size", self._on_extent_changed)

        self.scroll_down_btn = Gtk.Button(
            icon_name="go-bottom-symbolic",
            tooltip_text="Zur neuesten Nachricht",
            halign=Gtk.Align.END,
            valign=Gtk.Align.END,
            visible=False,
        )
        self.scroll_down_btn.add_css_class("osd")
        self.scroll_down_btn.add_css_class("circular")
        self.scroll_down_btn.add_css_class("scroll-down-button")
        self.scroll_down_btn.connect("clicked", lambda _b: self.scroll_to_bottom())

        overlay = Gtk.Overlay(child=self.scrolled_window)
        overlay.add_overlay(self.scroll_down_btn)
        self.set_child(overlay)

    def _on_setup(self, _factory, list_item: Gtk.ListItem):
        list_item.set_activatable(False)
        list_item.set_selectable(False)
        list_item.set_focusable(False)
        list_item.set_child(self._create_row())

    # --- Public API -----------------------------------------------------------

    def replace(self, items: Sequence[GObject.Object]):
        """Replace all items and jump to the newest one."""
        self.store.splice(0, self.store.get_n_items(), list(items))
        self.scroll_to_bottom()

    def append(self, item: GObject.Object):
        """Append an item; the view follows it only while at the bottom.

        To follow an own, just sent message, call scroll_to_bottom() before.
        """
        self.store.append(item)
        if self._stick_to_bottom:
            self.scroll_to_bottom()

    def scroll_to_bottom(self):
        self._stick_to_bottom = True
        self.scroll_down_btn.set_visible(False)
        self._scroll_to_last()

    # --- Scroll tracking ------------------------------------------------------

    def _scroll_to_last(self):
        n_items = self.store.get_n_items()
        if n_items:
            self.list_view.scroll_to(n_items - 1, Gtk.ListScrollFlags.NONE, Gtk.ScrollInfo())

    def _on_extent_changed(self, _adj: Gtk.Adjustment, _pspec):
        if self._stick_to_bottom and not self._rescroll_source:
            self._rescroll_source = GLib.idle_add(self._rescroll_after_layout)

    def _rescroll_after_layout(self) -> bool:
        self._rescroll_source = 0
        self._scroll_to_last()
        return GLib.SOURCE_REMOVE

    def _on_scrolled(self, adj: Gtk.Adjustment):
        if self._rescroll_source:
            return  # re-anchor pending, position is transient
        # Ignore values while the view is hidden or not yet laid out.
        if not self.list_view.get_mapped() or adj.get_page_size() <= 0:
            return
        distance = adj.get_upper() - adj.get_page_size() - adj.get_value()
        self._stick_to_bottom = distance <= STICKY_THRESHOLD
        self.scroll_down_btn.set_visible(not self._stick_to_bottom)
