"""Dialog to start a new chat: enter an address or link, scan a QR code, or
show the own address as QR code."""

from typing import Callable, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from retchat.contact_uri import Contact, parse_contact
from retchat.qr import decode_image_file
from retchat.widgets.qr_code_view import QrCodeView

QR_ICON = "org.selfmade.Retchat-qr-symbolic"


class NewChatDialog(Adw.Window):
    """``on_chat_created(destination hash, nickname)`` is called on start.

    ``service`` provides the own address (``contact_uri``,
    ``delivery_destination_hex``, ``display_name``) and ``learn_contact``.
    ``scanner_source`` replaces the camera (tests).
    """

    def __init__(self, parent_window: Gtk.Window, on_chat_created: Callable[[str, Optional[str]], None],
                 service=None, scanner_source: Optional[str] = None):
        super().__init__(transient_for=parent_window, modal=True)
        self.on_chat_created = on_chat_created
        self.service = service
        self._scanner_source = scanner_source
        self._scanned: Optional[Contact] = None  # last scanned contact (may carry its key)
        self._last_rejected = ""
        self.scanner = None

        self.set_title("Neuer Chat")
        self.set_default_size(400, 600)
        self.set_size_request(280, 360)

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        self.nav.add(self._build_main_page())
        self.connect("close-request", lambda _w: self._stop_scanner() or False)

        self._on_input_changed(None)

    # --- Pages ------------------------------------------------------------------

    def _build_main_page(self) -> Adw.NavigationPage:
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel_btn = Gtk.Button(label="Abbrechen")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)
        self.start_btn = Gtk.Button(label="Starten")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", self._on_start_clicked)
        header.pack_end(self.start_btn)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Kontakt")
        group.set_description("LXMF-Adresse (32 Hex-Zeichen) oder ein Kontakt-Link (lxmf://, lxma://)")

        self.hash_row = Adw.EntryRow(title="Adresse oder Kontakt-Link")
        self.hash_row.connect("changed", self._on_input_changed)
        self.hash_row.connect("entry-activated", self._on_start_clicked)
        paste_btn = Gtk.Button(icon_name="edit-paste-symbolic", tooltip_text="Einfügen", valign=Gtk.Align.CENTER)
        paste_btn.add_css_class("flat")
        paste_btn.connect("clicked", lambda _b: self._paste())
        self.hash_row.add_suffix(paste_btn)
        group.add(self.hash_row)

        self.name_row = Adw.EntryRow(title="Name (optional)")
        group.add(self.name_row)
        page.add(group)

        self.status_label = Gtk.Label(wrap=True, xalign=0.0, margin_top=8)
        self.status_label.add_css_class("dim-label")
        group.add(self.status_label)

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
        return Adw.NavigationPage(title="Neuer Chat", tag="new", child=view)

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
        return Adw.NavigationPage(title="Meine Adresse", tag="own", child=view)

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
            contact = parse_contact(text)
        except ValueError as e:
            if text != self._last_rejected:
                self._last_rejected = text
                self.scanner.status.set_text(f"{e}. Weiter suchen…")
            return
        own = self.service.delivery_destination_hex if self.service is not None else None
        if contact.destination_hash == own:
            self.scanner.status.set_text("Das ist deine eigene Adresse. Weiter suchen…")
            return
        self._use_contact(contact)

    def _use_contact(self, contact: Contact):
        self._stop_scanner()
        self._scanned = contact
        self.hash_row.set_text(contact.destination_hash)
        if self.nav.get_visible_page().get_tag() != "new":
            self.nav.pop_to_tag("new")
        self.name_row.grab_focus()

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
                contact = parse_contact(text)
            except ValueError as e:
                errors.append(str(e))
                continue
            self._use_contact(contact)
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

    def _current_contact(self) -> Contact:
        contact = parse_contact(self.hash_row.get_text())
        # A scanned lxma:// link carries the key; the field only shows the address
        if self._scanned is not None and self._scanned.destination_hash == contact.destination_hash:
            return self._scanned
        return contact

    def _on_input_changed(self, _entry):
        text = "".join(self.hash_row.get_text().split()).lower()
        if not text:
            self.start_btn.set_sensitive(False)
            self.status_label.set_text("")
            return
        if all(c in "0123456789abcdef" for c in text) and len(text) < 32:
            self.start_btn.set_sensitive(False)
            self.status_label.set_text(f"Länge: {len(text)}/32 Zeichen")
            return
        try:
            contact = self._current_contact()
        except ValueError as e:
            self.start_btn.set_sensitive(False)
            self.status_label.set_text(str(e))
            return
        self.start_btn.set_sensitive(True)
        if contact.public_key is not None:
            self.status_label.set_text("Gültige Adresse mit Schlüssel: Nachrichten können sofort "
                                       "verschlüsselt gesendet werden")
        else:
            self.status_label.set_text("Gültige Adresse")

    def _on_start_clicked(self, _widget):
        if not self.start_btn.get_sensitive():
            return
        try:
            contact = self._current_contact()
        except ValueError:
            return
        if contact.public_key is not None and self.service is not None:
            self.service.learn_contact(contact)
        nickname = self.name_row.get_text().strip() or None
        self._stop_scanner()
        self.close()
        self.on_chat_created(contact.destination_hash, nickname)

    # --- Helpers --------------------------------------------------------------------

    def _copy(self, text: str, message: str):
        self.get_clipboard().set(text)
        self._toast(message)

    def _toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))
