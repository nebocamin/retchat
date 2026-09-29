"""Browser for NomadNet pages, shown in place of the chat view."""

import re
from typing import Callable, List, Optional

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from retchat import micron
from retchat.micron import Link
from retchat.nomadnet_pages import URL_SCHEME, PageRequest, PageResult, parse_url, store_cached, uncache
from retchat.widgets.micron_view import MicronView

_LXMF_HASH_RE = re.compile(r"[0-9a-fA-F]{32}")


class PageView(Adw.Bin):
    """Shows pages of one NomadNet node at a time, with back/forward history.

    ``service`` provides ``page_fetcher``, ``node_name()``, ``own_node_hex``
    and ``operator_address()``. ``on_open_chat(lxmf_hash)`` opens a chat
    (lxmf@ links, "message the operator").
    """

    def __init__(self, service, on_back_clicked: Callable[[], None], on_open_chat: Callable[[str], None]):
        super().__init__(hexpand=True, vexpand=True)
        self.service = service
        self._on_back_clicked = on_back_clicked
        self._on_open_chat = on_open_chat
        self._collapsed = False

        self._history: List[PageRequest] = []
        self._index = -1
        self._loading: Optional[PageRequest] = None
        self._doc: Optional[micron.Document] = None

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(self._build_header())
        toolbar_view.set_content(self._build_content())
        self.set_child(toolbar_view)

        self._setup_actions()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self._rerender())

    # --- UI construction ------------------------------------------------------------------

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar()
        self.back_btn = Gtk.Button()
        self.back_btn.connect("clicked", lambda _b: self._on_back_button())
        header.pack_start(self.back_btn)
        self.nav_back_btn = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Zurück (Alt+←)",
                                       action_name="page.back")
        header.pack_start(self.nav_back_btn)
        self.nav_forward_btn = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Vor (Alt+→)",
                                          action_name="page.forward")
        header.pack_start(self.nav_forward_btn)

        self.window_title = Adw.WindowTitle(title="NomadNet")
        header.set_title_widget(self.window_title)

        menu = Gio.Menu()
        page_section = Gio.Menu()
        page_section.append("Neu laden", "page.reload")
        page_section.append("Adresse kopieren", "page.copy-url")
        menu.append_section(None, page_section)
        node_section = Gio.Menu()
        node_section.append("Betreiber anschreiben", "page.contact-operator")
        menu.append_section(None, node_section)
        header.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="Optionen", menu_model=menu))
        self._update_back_button()
        return header

    def _build_content(self) -> Gtk.Widget:
        self.micron_view = MicronView(on_link=self._on_link)

        spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        self.loading_page = Adw.StatusPage(title="Seite wird geladen", child=spinner)
        self.loading_page.add_css_class("compact")

        self.error_page = Adw.StatusPage(icon_name="network-offline-symbolic", title="Seite nicht erreichbar")
        retry = Gtk.Button(label="Erneut versuchen", halign=Gtk.Align.CENTER, action_name="page.reload")
        retry.add_css_class("pill")
        retry.add_css_class("suggested-action")
        self.error_page.set_child(retry)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.loading_page, "loading")
        self.stack.add_named(self.error_page, "error")
        self.stack.add_named(self.micron_view, "page")

        # Progress while a new page loads over the shown one
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.START, visible=False)
        self.progress.add_css_class("osd")
        overlay = Gtk.Overlay(child=self.stack)
        overlay.add_overlay(self.progress)
        return overlay

    def _setup_actions(self):
        group = Gio.SimpleActionGroup()
        self._actions = {}
        for name, handler in (("back", lambda: self._go(-1)), ("forward", lambda: self._go(1)),
                              ("reload", self.reload), ("copy-url", self._copy_url),
                              ("contact-operator", self._contact_operator)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, fn=handler: fn())
            group.add_action(action)
            self._actions[name] = action
        self.insert_action_group("page", group)

        shortcuts = Gtk.ShortcutController()
        for trigger, action in (("<Alt>Left", "page.back"), ("<Alt>Right", "page.forward"),
                                ("F5", "page.reload"), ("<Control>r", "page.reload")):
            shortcuts.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                                    Gtk.NamedAction.new(action)))
        self.add_controller(shortcuts)
        self._update_actions()

    # --- Public API -------------------------------------------------------------------------

    def open_node(self, node_hex: str, path: Optional[str] = None):
        """Start browsing a node (its index page, or ``path``) with an empty history."""
        self.open_request(PageRequest(bytes.fromhex(node_hex), path or "/page/index.mu"))

    def open_request(self, request: PageRequest):
        """Start browsing at ``request`` (e.g. a link from a chat) with an empty history."""
        self._history, self._index = [], -1
        self._doc = None
        self.micron_view.clear()
        self._navigate(request)

    def leave(self):
        """The view is no longer shown: stop loading and close the link."""
        self._loading = None
        self.progress.set_visible(False)
        self.service.page_fetcher.cancel()

    def reload(self):
        request = self._loading or self.current_request
        if request is not None:
            uncache(self.service.app.cachepath, request.url)
            # A page that failed to load isn't in the history yet.
            self._navigate(request, push=request is not self.current_request, use_cache=False)

    @property
    def current_request(self) -> Optional[PageRequest]:
        return self._history[self._index] if 0 <= self._index < len(self._history) else None

    def set_back_button_mode(self, is_collapsed: bool):
        self._collapsed = is_collapsed
        self._update_back_button()

    # --- Navigation ---------------------------------------------------------------------

    def _navigate(self, request: PageRequest, push: bool = True, use_cache: bool = True, index: Optional[int] = None):
        self._loading = request
        self._update_title(request, "Wird geladen…")
        if self._doc is None:
            self.loading_page.set_description(None)
            self.stack.set_visible_child_name("loading")
        else:
            self.progress.set_fraction(0.0)
            self.progress.set_visible(True)
        self.service.page_fetcher.fetch(
            request,
            on_status=lambda text, fraction: self._on_status(request, text, fraction),
            on_done=lambda result: self._on_done(result, push, index),
            use_cache=use_cache,
        )

    def _go(self, step: int):
        target = self._index + step
        if 0 <= target < len(self._history):
            self._navigate(self._history[target], push=False, index=target)

    def _on_status(self, request: PageRequest, text: str, fraction: Optional[float]):
        if request is not self._loading:
            return
        self._update_title(request, text)
        self.loading_page.set_description(text)
        if fraction is not None:
            self.progress.set_fraction(fraction)
        else:
            self.progress.pulse()

    def _on_done(self, result: PageResult, push: bool, index: Optional[int]):
        if result.request is not self._loading:
            return
        self._loading = None
        self.progress.set_visible(False)
        request = result.request

        if result.error or result.data is None:
            error = result.error or "Unbekannter Fehler"
            if self._doc is None:
                self.error_page.set_description(error)
                self.stack.set_visible_child_name("error")
                self._update_title(request, "Nicht erreichbar")
            else:
                self._update_title(self.current_request or request)
                self._toast(error)
            self._update_actions()
            return

        markup = result.data.decode("utf-8", errors="replace")
        doc = micron.parse(markup)
        if not result.from_cache and request.data is None:
            store_cached(self.service.app.cachepath, request.url, result.data, doc.cache_time)

        if push:
            del self._history[self._index + 1:]
            self._history.append(request)
            self._index = len(self._history) - 1
        elif index is not None:
            self._index = index
        self._doc = doc
        self._rerender()
        self.stack.set_visible_child_name("page")
        # Also after the stack switch has settled (GTK may move the focus then)
        self.micron_view.take_focus()
        GLib.idle_add(lambda: self.micron_view.take_focus() and False)
        self._update_title(request)
        self._update_actions()

        anchor = (request.data or {}).get("var_anchor")
        GLib.idle_add(lambda: self.micron_view.scroll_to_anchor(anchor) and False)

    def _rerender(self):
        if self._doc is not None:
            self.micron_view.render(self._doc, Adw.StyleManager.get_default().get_dark())

    # --- Links --------------------------------------------------------------------------

    def _on_link(self, link: Link):
        url = link.url.strip()
        current = self.current_request
        if url.startswith("#"):
            if url[1:] and not self.micron_view.has_anchor(url[1:]):
                self._toast(f"Unbekannter Sprungpunkt: {url}")
            else:
                self.micron_view.scroll_to_anchor(url[1:] or None)
            return
        if (re.match(r"[a-z][a-z0-9+.-]*://", url, re.IGNORECASE)
                and not url.lower().startswith(("rrc://", URL_SCHEME))):
            # Web links would leave the mesh (and reveal the reader); copy only.
            Gdk.Display.get_default().get_clipboard().set(url)
            self._toast(f"Externer Link kopiert: {url}")
            return

        prefix, sep, rest = url.partition("@")
        if sep and prefix == "lxmf":
            address = rest.split(":")[0].strip()
            if _LXMF_HASH_RE.fullmatch(address):
                self._on_open_chat(address.lower())
            else:
                self._toast("Ungültige LXMF-Adresse")
            return
        if sep and prefix != "nnn":
            self._toast(f"Links vom Typ «{prefix}» werden noch nicht unterstützt")
            return
        if url.startswith("p:") or url.startswith("rrc://"):
            self._toast("Dieser Link-Typ wird noch nicht unterstützt")
            return

        data = self.micron_view.form_data(link.fields)
        for item in link.fields:
            key, eq, value = item.partition("=")
            if eq and key:
                data["var_" + key] = value
        try:
            request = parse_url(url, current.node if current else None, data or None)
        except ValueError as e:
            self._toast(str(e))
            return
        if request.is_file:
            self._toast("Datei-Downloads werden noch nicht unterstützt")
            return
        self._navigate(request)

    # --- Header -------------------------------------------------------------------------

    def _node_title(self, request: PageRequest) -> str:
        node_hex = request.node_hex
        if node_hex == self.service.own_node_hex:
            return self.service.node_name(node_hex) or "Deine Node"
        return self.service.node_name(node_hex) or f"{node_hex[:8]}…{node_hex[-4:]}"

    def _update_title(self, request: PageRequest, status: Optional[str] = None):
        self.window_title.set_title(self._node_title(request))
        self.window_title.set_subtitle(status or request.path)

    def _update_actions(self):
        self._actions["back"].set_enabled(self._index > 0)
        self._actions["forward"].set_enabled(self._index < len(self._history) - 1)
        request = self.current_request or self._loading
        self._actions["reload"].set_enabled(request is not None)
        self._actions["copy-url"].set_enabled(request is not None)
        own = request is not None and request.node_hex == self.service.own_node_hex
        self._actions["contact-operator"].set_enabled(request is not None and not own)
        self._update_back_button()

    def _update_back_button(self):
        # Narrow (mobile): one back button, first through the history, then to
        # the chat list. Wide: sidebar toggle plus separate history buttons.
        if self._collapsed:
            self.back_btn.set_icon_name("go-previous-symbolic")
            can_go_back = self._index > 0
            self.back_btn.set_tooltip_text("Zurück" if can_go_back else "Zurück zur Übersicht")
            self.nav_back_btn.set_visible(False)
            self.nav_forward_btn.set_visible(self._index < len(self._history) - 1)
        else:
            self.back_btn.set_icon_name("sidebar-show-symbolic")
            self.back_btn.set_tooltip_text("Seitenleiste ein-/ausblenden")
            self.nav_back_btn.set_visible(True)
            self.nav_forward_btn.set_visible(True)

    def _on_back_button(self):
        if self._collapsed and self._index > 0:
            self._go(-1)
        else:
            self._on_back_clicked()

    def _copy_url(self):
        request = self.current_request or self._loading
        if request is not None:
            Gdk.Display.get_default().get_clipboard().set(request.url)
            self._toast("Adresse kopiert")

    def _contact_operator(self):
        request = self.current_request or self._loading
        if request is None:
            return
        address = self.service.operator_address(request.node_hex)
        if address:
            self._on_open_chat(address)
        else:
            self._toast("Die Adresse des Betreibers ist nicht bekannt")

    def _toast(self, text: str):
        overlay = self.get_ancestor(Adw.ToastOverlay)
        if overlay is not None:
            overlay.add_toast(Adw.Toast(title=text, timeout=3))
