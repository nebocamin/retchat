"""Row for a saved NomadNet node in the sidebar ("Seiten")."""

from typing import Any, Dict

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, GLib, Gtk

from retchat.widgets.announce_row import NODE_ICON


class NodeRow(Adw.ActionRow):
    """Activating the row (list box "row-activated") opens the node's pages."""

    def __init__(self, node: Dict[str, Any]):
        super().__init__(activatable=True)
        self.dest_hash = node["destination_hash"]
        self.name = node.get("name") or ""
        self.add_prefix(Adw.Avatar(size=40, show_initials=False, icon_name=NODE_ICON))
        self.hops_label = Gtk.Label(valign=Gtk.Align.CENTER)
        self.hops_label.add_css_class("badge")
        self.hops_label.add_css_class("hops-badge")
        self.add_suffix(self.hops_label)
        self.update(node)

    def update(self, node: Dict[str, Any]):
        self.name = node.get("name") or ""
        dest = self.dest_hash
        self.set_title(GLib.markup_escape_text(self.name or f"{dest[:8]}…{dest[-4:]}"))
        self.set_subtitle(f"{dest[:12]}…")
        hops = node.get("hops")
        self.hops_label.set_visible(hops is not None)
        if hops is not None:
            self.hops_label.set_text("Lokal" if hops == 0 else f"{hops} Hop{'s' if hops > 1 else ''}")
