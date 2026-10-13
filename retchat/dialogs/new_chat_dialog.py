"""Dialog behind the "+" button: open an address (chat or NomadNet page),
entered, pasted or scanned as QR code; or show the own address as QR code.

What an address is (contact, node, ...) is recognised from the identity
RNS knows for it; an unknown address is searched in the network first, and
if it isn't found, the user chooses.
"""

import re
from typing import Callable, Optional, Tuple, Union

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from retchat.contact_uri import Contact, parse_contact
from retchat.nomadnet_pages import URL_SCHEME, PageRequest, parse_url
from retchat.qr import decode_image_file
from retchat.widgets.qr_code_view import QrCodeView

QR_ICON = "org.selfmade.Retchat-qr-symbolic"
# Seconds an unknown address is searched in the network before asking
SEARCH_SECONDS = 15
_PAGE_URL_RE = re.compile(r"[0-9a-fA-F]{32}:/\S*")


def parse_target(text: str) -> Union[PageRequest, Contact]:
    """A NomadNet page link, or an address / contact link; raises ValueError."""
    value = (text or "").strip()
    if value.lower().startswith(URL_SCHEME) or _PAGE_URL_RE.fullmatch(value):
        return parse_url(value)
    return parse_contact(value)


class NewChatDialog(Adw.Window):
    """``on_chat_created(destination hash, nickname)`` starts a chat,
    ``on_open_page(url)`` opens a NomadNet page (a node address opens its
    index page; nothing is saved).

    ``service`` provides the own address (``contact_uri``,
    ``delivery_destination_hex``, ``display_name``), ``learn_contact``,
    ``address_kind``, ``addresses_for_identity`` and ``request_path``.
    ``scanner_source`` replaces the camera (tests).
    """

    def __init__(self, parent_window: Gtk.Window, on_chat_created: Callable[[str, Optional[str]], None],
                 service=None, scanner_source: Optional[str] = None,
                 on_open_page: Optional[Callable[[str], None]] = None):
        super().__init__(transient_for=parent_window, modal=True)
        self.on_chat_created = on_chat_created
        self.on_open_page = on_open_page
        self.service = service
        self._scanner_source = scanner_source
        self._scanned: Optional[Contact] = None  # last scanned contact (may carry its key)
        self._last_rejected = ""
        self.scanner = None
        self.kind = "none"  # what the entered address is, see _apply_kind
        self._target: Optional[Union[PageRequest, Contact]] = None
        self._search_source = 0
        self._search_deadline = 0.0

        self.set_title("Adresse öffnen")
        self.set_default_size(400, 600)
        self.set_size_request(280, 360)

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        self.nav.add(self._build_main_page())
        self.connect("close-request", lambda _w: (self._stop_scanner(), self._stop_search()) and False)

        self._on_input_changed(None)

    # --- Pages ------------------------------------------------------------------

    def _build_main_page(self) -> Adw.NavigationPage:
        # Only the close button (and Esc): two text buttons ("Abbrechen",
        # "Seite öffnen") don't fit a 360 px header bar with larger text.
        header = Adw.HeaderBar()
        self.start_btn = Gtk.Button(label="Öffnen", halign=Gtk.Align.CENTER, margin_top=12)
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.add_css_class("pill")
        self.start_btn.connect("clicked", self._on_start_clicked)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Adresse")
        group.set_description("Adresse eines Kontakts oder einer NomadNet-Node (32 Hex-Zeichen), "
                              "oder ein Link (lxmf://, lxma://, nomadnetwork://)")

        self.hash_row = Adw.EntryRow(title="Adresse oder Link")
        self.hash_row.connect("changed", self._on_input_changed)
        self.hash_row.connect("entry-activated", self._on_start_clicked)
        paste_btn = Gtk.Button(icon_name="edit-paste-symbolic", tooltip_text="Einfügen", valign=Gtk.Align.CENTER)
        paste_btn.add_css_class("flat")
        paste_btn.connect("clicked", lambda _b: self._paste())
        self.hash_row.add_suffix(paste_btn)
        group.add(self.hash_row)

        # Unknown or ambiguous address: the user says what it is
        self.kind_toggle = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        self.kind_toggle.add(Adw.Toggle(name="chat", label="Chat"))
        self.kind_toggle.add(Adw.Toggle(name="page", label="Seite"))
        self.kind_toggle.set_active_name("chat")
        self.kind_toggle.connect("notify::active-name", lambda *_: self._update_for_choice())
        self.kind_row = Adw.ActionRow(title="Öffnen als", visible=False)
        self.kind_row.add_suffix(self.kind_toggle)
        group.add(self.kind_row)

        self.name_row = Adw.EntryRow(title="Name (optional)")
        group.add(self.name_row)
        page.add(group)

        status = Gtk.Box(spacing=8, margin_top=8)
        self.search_spinner = Adw.Spinner(visible=False)
        status.append(self.search_spinner)
        self.status_label = Gtk.Label(wrap=True, xalign=0.0, hexpand=True)
        self.status_label.add_css_class("dim-label")
        status.append(self.status_label)
        group.add(status)
        group.add(self.start_btn)

        qr_group = Adw.PreferencesGroup(title="QR-Code")
        scan_row = Adw.ActionRow(title="QR-Code scannen", subtitle="Mit der Kamera oder aus einem Bild",
                                 activatable=True)
        scan_row.add_prefix(Gtk.Image(icon_name="camera-photo-symbolic"))
        scan_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        scan_row.connect("activated", lambda _r: self.show_scanner())
        qr_group.add(scan_row)
        if self.service is not None:
            own_row = Adw.ActionRow(title="Meine Adresse zeigen", subtitle="Als QR-Code, zum Scannen für andere",
                                    activatable=True)
            own_row.add_prefix(Gtk.Image(icon_name=QR_ICON))
            own_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            own_row.connect("activated", lambda _r: self.show_own_code())
            qr_group.add(own_row)
        page.add(qr_group)

        view = Adw.ToolbarView(content=page)
        view.add_top_bar(header)
        return Adw.NavigationPage(title="Adresse öffnen", tag="new", child=view)

    def _build_scan_page(self) -> Adw.NavigationPage:
        from retchat.widgets.qr_scanner import QrScanner  # loads GStreamer only when needed
        self.scanner = QrScanner(self._on_scanned, source=self._scanner_source)
        self.scanner.set_margin_start(12)
        self.scanner.set_margin_end(12)
        self.scanner.set_margin_top(6)

        image_btn = Gtk.Button(label="Aus Bild laden…", halign=Gtk.Align.CENTER, margin_bottom=18)
        image_btn.add_css_class("pill")
        image_btn.connect("clicked", lambda _b: self._choose_image())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.scanner)
        box.append(image_btn)

        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.NavigationPage(title="QR-Code scannen", tag="scan", child=view)
        # The camera only runs while the page is shown
        page.connect("shown", lambda _p: self.scanner.start())
        page.connect("hiding", lambda _p: self._stop_scanner())
        return page

    def _build_own_page(self) -> Adw.NavigationPage:
        uri = self.service.contact_uri
        address = self.service.delivery_destination_hex

        self.own_code = QrCodeView(uri)
        self.own_code.set_hexpand(True)
        self.own_code.set_vexpand(True)
        self.own_code.set_size_request(200, 200)

        name = Gtk.Label(label=self.service.display_name or "", wrap=True, justify=Gtk.Justification.CENTER)
        name.add_css_class("title-2")
        # Not selectable: "Adresse kopieren" copies it
        addr = Gtk.Label(label=address, wrap=True, justify=Gtk.Justification.CENTER)
        addr.add_css_class("monospace")
        hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                         label="Enthält deine Adresse und deinen öffentlichen Schlüssel: wer den Code scannt, "
                               "kann dir sofort schreiben (Retchat, MeshChatX und andere).")
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")

        copy_addr = Gtk.Button(label="Adresse kopieren")
        copy_addr.connect("clicked", lambda _b: self._copy(address, "Adresse kopiert"))
        copy_link = Gtk.Button(label="Link kopieren")
        copy_link.connect("clicked", lambda _b: self._copy(uri, "Kontakt-Link kopiert"))
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, homogeneous=True)
        for btn in (copy_addr, copy_link):
            btn.add_css_class("pill")
            buttons.append(btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=18, margin_end=18,
                      margin_top=6, margin_bottom=18)
        for widget in (self.own_code, name, addr, hint, buttons):
            box.append(widget)

        view = Adw.ToolbarView(content=box)
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.NavigationPage(title="Meine Adresse", tag="own", child=view)
        # Whoever scans the code needs a path to us, not just the key: announce,
        # so the network has a fresh one (see announce_for_contact_exchange).
        page.connect("shown", lambda _p: self._announce_for_exchange())
        return page

    # --- Navigation ---------------------------------------------------------------

    def show_scanner(self):
        if self.nav.find_page("scan") is None:
            self.nav.add(self._build_scan_page())
        self.nav.push_by_tag("scan")

    def show_own_code(self):
        if self.service is None:
            return
        if self.nav.find_page("own") is None:
            self.nav.add(self._build_own_page())
        self.nav.push_by_tag("own")

    def _stop_scanner(self):
        if self.scanner is not None:
            self.scanner.stop()

    # --- Scanning -----------------------------------------------------------------

    def _on_scanned(self, text: str):
        try:
            target = parse_target(text)
        except ValueError as e:
            if text != self._last_rejected:
                self._last_rejected = text
                self.scanner.status.set_text(f"{e}. Weiter suchen…")
            return
        own = self.service.delivery_destination_hex if self.service is not None else None
        if isinstance(target, Contact) and target.destination_hash == own:
            self.scanner.status.set_text("Das ist deine eigene Adresse. Weiter suchen…")
            return
        self._use_target(target, text)

    def _use_target(self, target: Union[PageRequest, Contact], text: str):
        self._stop_scanner()
        if isinstance(target, Contact):
            self._scanned = target
            self.hash_row.set_text(target.destination_hash)
        else:
            self.hash_row.set_text(text.strip())
        if self.nav.get_visible_page().get_tag() != "new":
            self.nav.pop_to_tag("new")
        (self.name_row if self.name_row.get_visible() else self.start_btn).grab_focus()

    def _choose_image(self):
        images = Gtk.FileFilter(name="Bilder")
        images.add_mime_type("image/*")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(images)
        dialog = Gtk.FileDialog(title="Bild mit QR-Code öffnen", filters=filters, default_filter=images)
        dialog.open(self, None, self._on_image_chosen)

    def _on_image_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # dismissed
        if file and file.get_path():
            self.scan_image(file.get_path())

    def scan_image(self, path: str):
        try:
            texts = decode_image_file(path)
        except Exception:
            self._toast("Das Bild konnte nicht gelesen werden")
            return
        if not texts:
            self._toast("Kein QR-Code im Bild gefunden")
            return
        errors = []
        for text in texts:
            try:
                target = parse_target(text)
            except ValueError as e:
                errors.append(str(e))
                continue
            self._use_target(target, text)
            return
        self._toast(errors[0])

    # --- Main page ------------------------------------------------------------------

    def _paste(self):
        Gdk.Display.get_default().get_clipboard().read_text_async(None, self._on_pasted)

    def _on_pasted(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult):
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:
            return
        if text:
            self.hash_row.set_text(text.strip())

    def _current_target(self) -> Union[PageRequest, Contact]:
        target = parse_target(self.hash_row.get_text())
        # A scanned lxma:// link carries the key; the field only shows the address
        if (isinstance(target, Contact) and self._scanned is not None
                and self._scanned.destination_hash == target.destination_hash):
            return self._scanned
        return target

    def _on_input_changed(self, _entry):
        self._stop_search()
        self._target = None
        text = "".join(self.hash_row.get_text().split()).lower()
        if not text:
            self._apply_kind("none", "")
            return
        if all(c in "0123456789abcdef" for c in text) and len(text) < 32:
            self._apply_kind("none", f"Länge: {len(text)}/32 Zeichen")
            return
        try:
            target = self._current_target()
        except ValueError as e:
            self._apply_kind("none", str(e))
            return
        self._target = target
        if isinstance(target, PageRequest) and target.is_file:
            name = self._node_name(target.node_hex)
            self._apply_kind("file", f"Datei von {name}: {target.path.rsplit('/', 1)[-1]}")
        elif isinstance(target, PageRequest):
            name = self._node_name(target.node_hex)
            self._apply_kind("page", f"NomadNet-Seite: {name} · {target.path}")
        elif target.public_key is not None:
            # lxma:// link: an LXMF address, verified against its key
            self._apply_kind("contact", "Kontakt mit Schlüssel: Nachrichten können sofort verschlüsselt "
                                        "gesendet werden")
        elif self.service is None:
            self._apply_kind("contact", "Gültige Adresse")
        elif not self._recognise(target.destination_hash):
            self._search()

    def _recognise(self, dest_hex: str) -> bool:
        kind, name = self.service.address_kind(dest_hex)
        if kind == "contact":
            self._apply_kind("contact", f"Kontakt: {name}" if name else "Kontakt")
        elif kind == "node":
            self._apply_kind("node", f"NomadNet-Seite: {name}" if name else "NomadNet-Seite")
        elif kind == "propagation":
            self._apply_kind("propagation", "Propagation-Node: speichert Nachrichten für andere, "
                                            "hat weder Seiten noch einen Chat")
        elif kind == "identity":
            self._apply_kind("choose", "Identität: dazu gehören ein Kontakt und eine Node")
        else:
            return False
        return True

    def _node_name(self, node_hex: str) -> str:
        name = self.service.node_name(node_hex) if self.service is not None else None
        return name or f"{node_hex[:8]}…{node_hex[-4:]}"

    # --- Searching unknown addresses ---------------------------------------------------

    def _search(self):
        """Not heard of yet: ask the network (an announce or path response
        teaches the identity), then recognise it, or let the user choose."""
        dest_hex = self._target.destination_hash
        self._apply_kind("searching", "Wird im Netz gesucht…")
        self.service.request_path(dest_hex)
        self._search_deadline = GLib.get_monotonic_time() / 1e6 + SEARCH_SECONDS
        self._search_source = GLib.timeout_add_seconds(1, self._search_tick, dest_hex)

    def _search_tick(self, dest_hex: str) -> bool:
        if self._recognise(dest_hex):
            self._search_source = 0
            return False
        if GLib.get_monotonic_time() / 1e6 < self._search_deadline:
            return True
        self._search_source = 0
        self._apply_kind("choose", "Im Netz noch nicht gefunden. Was ist die Adresse?")
        return False

    def _stop_search(self):
        if self._search_source:
            GLib.source_remove(self._search_source)
            self._search_source = 0

    # --- State -----------------------------------------------------------------------------

    def _apply_kind(self, kind: str, status: str):
        """kind: none (invalid/empty), page, file, contact, node,
        propagation, searching, choose (toggle shown)."""
        self.kind = kind
        self.status_label.set_text(status)
        self.search_spinner.set_visible(kind == "searching")
        self.kind_row.set_visible(kind == "choose")
        if kind == "choose":
            self._update_for_choice()
            return
        action = {"contact": "chat", "node": "page", "page": "page", "file": "file"}.get(kind)
        self._set_action(action)

    def _update_for_choice(self):
        if self.kind == "choose":
            self._set_action(self.kind_toggle.get_active_name())

    def _set_action(self, action: Optional[str]):
        """What the start button does: "chat", "page" or nothing."""
        self._action = action
        self.start_btn.set_sensitive(action is not None)
        self.start_btn.set_label({"chat": "Chat starten", "page": "Seite öffnen",
                                  "file": "Herunterladen"}.get(action, "Öffnen"))
        self.name_row.set_visible(action not in ("page", "file"))

    def _resolved_address(self, action: str) -> str:
        """The address to open: for an identity hash, its chat or node address."""
        dest_hex = self._target.destination_hash
        if self.service is not None and self.service.address_kind(dest_hex)[0] == "identity":
            return self.service.addresses_for_identity(dest_hex).get("contact" if action == "chat" else "node",
                                                                     dest_hex)
        return dest_hex

    def _on_start_clicked(self, _widget):
        action = getattr(self, "_action", None)
        if action is None or self._target is None:
            return
        self._stop_scanner()
        self._stop_search()
        if isinstance(self._target, PageRequest):
            self.close()
            if self.on_open_page is not None:
                self.on_open_page(self._target.url)
            return
        address = self._resolved_address(action)
        if action == "page":
            self.close()
            if self.on_open_page is not None:
                self.on_open_page(address)
            return
        contact = self._target
        if contact.public_key is not None and self.service is not None:
            self.service.learn_contact(contact)
            # The other side can then verify our first message and reply
            self._announce_for_exchange()
        nickname = self.name_row.get_text().strip() or None
        self.close()
        self.on_chat_created(address, nickname)

    # --- Helpers --------------------------------------------------------------------

    def _announce_for_exchange(self):
        announce = getattr(self.service, "announce_for_contact_exchange", None)
        if announce is not None:
            announce()

    def _copy(self, text: str, message: str):
        self.get_clipboard().set(text)
        self._toast(message)

    def _toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))
