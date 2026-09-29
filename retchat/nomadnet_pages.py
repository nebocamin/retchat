"""Loading NomadNet pages from nodes ("nomadnetwork.node" destinations).

Follows NomadNet's Browser (nomadnet/ui/textui/Browser.py, GPLv3), without
its urwid UI: find a path to the node, open an RNS link, optionally identify,
and request the page path. Pages are cached in NomadNet's cache directory in
the same format, so Retchat and the NomadNet terminal client share it.
Pages of the own node (if Retchat runs one) are served locally.

URL format (as in NomadNet): ``<node hash>[:<path>][`var=value|var2=value2]``.
``:<path>`` without hash refers to the current node, a bare hash to its
index page.
"""

import inspect
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import RNS

APP_NAME = "nomadnetwork"
# Link form in messages (as written by MeshChat): nomadnetwork://<NomadNet URL>
URL_SCHEME = "nomadnetwork://"
_URL_IN_TEXT_RE = re.compile(r"nomadnetwork://[0-9a-fA-F]{32}(?::/\S*|`\S*)?", re.IGNORECASE)
# Punctuation that ends a sentence rather than belonging to the link
_TRAILING_PUNCTUATION = ".,;:!?)]}>'\"»“”"
ASPECT = "node"
DEFAULT_PATH = "/page/index.mu"
HASH_HEX_LENGTH = RNS.Reticulum.TRUNCATED_HASHLENGTH // 8 * 2
# Link establishment gets its own timeout from RNS; this is only a safety net.
LINK_WAIT_LIMIT = 90.0
# Seconds without any progress after which a request is given up
REQUEST_IDLE_LIMIT = 45.0


@dataclass
class PageRequest:
    node: bytes
    path: str = DEFAULT_PATH
    # "var_*" variables from the URL/link and "field_*" form values
    data: Optional[Dict[str, str]] = None

    @property
    def node_hex(self) -> str:
        return self.node.hex()

    @property
    def url(self) -> str:
        """URL of the page (form values are not part of it), like NomadNet's current_url()."""
        url = f"{self.node_hex}:{self.path}"
        variables = [f"{k[4:]}={v}" for k, v in (self.data or {}).items()
                     if isinstance(k, str) and k.startswith("var_")]
        if variables:
            url += "`" + "|".join(variables)
        return url

    @property
    def is_page(self) -> bool:
        return self.path.startswith("/page/")

    @property
    def is_file(self) -> bool:
        return self.path.startswith("/file/")


def parse_url(url: str, current_node: Optional[bytes] = None,
              data: Optional[Dict[str, str]] = None) -> PageRequest:
    """Parse a NomadNet URL; raises ValueError if it is malformed."""
    url = url.strip()
    data = dict(data) if data else {}
    if url.startswith("nnn@"):
        url = url[4:]
    elif url.lower().startswith(URL_SCHEME):
        url = url[len(URL_SCHEME):]
    if "`" in url:
        url, _, variables = url.partition("`")
        for item in variables.split("|"):
            key, sep, value = item.partition("=")
            if sep and key and "=" not in value:
                data["var_" + key] = value

    node_part, sep, path = url.partition(":")
    if node_part:
        if len(node_part) != HASH_HEX_LENGTH:
            raise ValueError(f"Ungültige Adresse: {url}")
        try:
            node = bytes.fromhex(node_part)
        except ValueError:
            raise ValueError(f"Ungültige Adresse: {url}") from None
    elif sep and current_node is not None:
        node = current_node
    else:
        raise ValueError(f"Ungültige Adresse: {url}")
    if not path:
        path = DEFAULT_PATH
    if not path.startswith("/"):
        raise ValueError(f"Ungültiger Pfad: {path}")
    return PageRequest(node, path, data or None)


def find_urls(text: str) -> List[Tuple[int, int, str]]:
    """``nomadnetwork://`` links in a text: (start, end, url) of each.

    Punctuation directly after a link ("see nomadnetwork://…/x.mu.") is not
    part of it; a closing parenthesis only if the link has no opening one.
    """
    found = []
    for match in _URL_IN_TEXT_RE.finditer(text):
        url = match.group(0)
        while url[-1] in _TRAILING_PUNCTUATION:
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            url = url[:-1]
        found.append((match.start(), match.start() + len(url), url))
    return found


# --- Cache (compatible with NomadNet's Browser) ------------------------------------------

def _url_hash(url: str) -> str:
    return RNS.hexrep(RNS.Identity.full_hash(url.encode("utf-8")), delimit=False)


def get_cached(cache_dir: str, url: str) -> Optional[bytes]:
    """Cached page data, removing expired entries on the way."""
    wanted = _url_hash(url)
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return None
    now = time.time()
    for name in names:
        prefix, sep, expires = name.partition("_")
        if not sep or len(prefix) != 64:
            continue
        path = os.path.join(cache_dir, name)
        try:
            if now > float(expires):
                os.unlink(path)
            elif prefix == wanted:
                with open(path, "rb") as f:
                    return f.read()
        except (OSError, ValueError):
            continue
    return None


def uncache(cache_dir: str, url: str):
    wanted = _url_hash(url)
    try:
        for name in os.listdir(cache_dir):
            if name.startswith(wanted + "_"):
                os.unlink(os.path.join(cache_dir, name))
    except OSError:
        pass


def store_cached(cache_dir: str, url: str, data: bytes, lifetime: float):
    if lifetime <= 0:
        return
    uncache(cache_dir, url)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, f"{_url_hash(url)}_{time.time() + lifetime}"), "wb") as f:
            f.write(data)
    except OSError as e:
        RNS.log(f"Retchat: Could not cache page {url}: {e}", RNS.LOG_WARNING)


# --- Fetching ------------------------------------------------------------------------------

@dataclass
class PageResult:
    request: PageRequest
    data: Optional[bytes] = None
    error: Optional[str] = None
    from_cache: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


class PageFetcher:
    """Loads pages in a worker thread; one request at a time.

    ``fetch()`` reports progress with ``on_status(text, fraction or None)``
    and the outcome with ``on_done(PageResult)``, both through ``dispatch``
    (GLib.idle_add in the app, so callbacks run on the main loop). Starting
    a new fetch makes the results of a running one be ignored.
    """

    def __init__(self, app, dispatch: Callable[..., Any]):
        self.app = app  # NomadNetworkApp
        self._dispatch = dispatch
        self._generation = 0
        self._lock = threading.Lock()
        self._link: Optional[RNS.Link] = None

    # --- Public API -------------------------------------------------------------------------

    def fetch(self, request: PageRequest,
              on_status: Callable[[str, Optional[float]], None],
              on_done: Callable[[PageResult], None],
              use_cache: bool = True):
        with self._lock:
            self._generation += 1
            generation = self._generation

        def report(fn, *args):
            if generation == self._generation:
                fn(*args)
            return False

        def status(text: str, fraction: Optional[float] = None):
            self._dispatch(report, on_status, text, fraction)

        def done(result: PageResult):
            self._dispatch(report, on_done, result)

        threading.Thread(target=self._run, args=(request, use_cache, status, done),
                         daemon=True, name=f"Page-{request.node_hex[:8]}").start()

    def cancel(self):
        """Ignore the running request and close the link to the node."""
        with self._lock:
            self._generation += 1
        self._teardown()

    def is_own_node(self, node: bytes) -> bool:
        own = getattr(self.app, "node", None)
        return own is not None and own.destination.hash == node

    # --- Worker ---------------------------------------------------------------------------

    def _run(self, request: PageRequest, use_cache: bool, status, done):
        try:
            cache_dir = self.app.cachepath
            if use_cache and request.data is None:
                cached = get_cached(cache_dir, request.url)
                if cached is not None:
                    done(PageResult(request, cached, from_cache=True))
                    return
            if self.is_own_node(request.node):
                done(self._serve_locally(request))
                return
            done(self._fetch_remote(request, status))
        except Exception as e:
            RNS.log(f"Retchat: Error loading page {request.url}: {e}", RNS.LOG_ERROR)
            done(PageResult(request, error=f"Fehler beim Laden: {e}"))

    def _serve_locally(self, request: PageRequest) -> PageResult:
        """A page of the own node, answered by its request handler without a link."""
        destination = self.app.node.destination
        handler = destination.request_handlers.get(RNS.Identity.truncated_hash(request.path.encode("utf-8")))
        if handler is None:
            return PageResult(request, error="Diese Seite gibt es auf deiner Node nicht.")
        generator = handler[1]
        args = [request.path, request.data, os.urandom(16)]
        if len(inspect.signature(generator).parameters) == 6:
            args.append(None)  # link id
        args += [self.app.identity, time.time()]
        data = generator(*args)
        if not isinstance(data, (bytes, bytearray)):
            return PageResult(request, error="Die Seite konnte nicht erzeugt werden.")
        return PageResult(request, bytes(data))

    def _path_timeout(self, node: bytes) -> float:
        rns = getattr(self.app, "rns", None)
        try:
            return max(15 + rns.get_first_hop_timeout(node), rns.get_medium_path_timeout())
        except Exception:
            return 30.0

    def _get_link(self, node: bytes, status) -> RNS.Link:
        link = self._link
        if link is not None and link.destination.hash == node and link.status == RNS.Link.ACTIVE:
            return link
        self._teardown()

        if not RNS.Transport.has_path(node):
            status("Pfad zur Node wird gesucht…")
            RNS.Transport.request_path(node)
            deadline = time.time() + self._path_timeout(node)
            while not RNS.Transport.has_path(node):
                if time.time() > deadline:
                    raise _FetchError("Kein Pfad zur Node gefunden. Sie ist vermutlich gerade nicht erreichbar.")
                time.sleep(0.25)

        identity = RNS.Identity.recall(node)
        if identity is None:
            raise _FetchError("Die Identität der Node ist unbekannt.")
        status("Verbindung wird aufgebaut…")
        destination = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME, ASPECT)
        settled = threading.Event()
        link = RNS.Link(destination,
                        established_callback=lambda _l: settled.set(),
                        closed_callback=lambda _l: settled.set())
        settled.wait(LINK_WAIT_LIMIT)
        if link.status != RNS.Link.ACTIVE:
            link.teardown()
            raise _FetchError("Die Verbindung zur Node konnte nicht aufgebaut werden.")

        directory = getattr(self.app, "directory", None)
        if directory is not None and directory.should_identify_on_connect(node):
            link.identify(self.app.identity)
        self._link = link
        return link

    def _fetch_remote(self, request: PageRequest, status) -> PageResult:
        try:
            link = self._get_link(request.node, status)
        except _FetchError as e:
            return PageResult(request, error=str(e))

        status("Seite wird angefordert…")
        finished = threading.Event()
        outcome: Dict[str, Any] = {}
        last_activity = [time.time()]

        def response(receipt):
            outcome["data"] = receipt.response
            finished.set()

        def failed(receipt):
            outcome["failed"] = True
            finished.set()

        def progress(receipt):
            last_activity[0] = time.time()
            try:
                status("Seite wird empfangen…", float(receipt.get_progress()))
            except Exception:
                pass

        receipt = link.request(request.path, data=request.data, response_callback=response,
                               failed_callback=failed, progress_callback=progress)
        if not receipt:
            self._teardown()
            return PageResult(request, error="Die Anfrage konnte nicht gesendet werden.")

        # RNS usually times the request out itself (failed callback), but not
        # always (seen with real nodes): give up after a while without progress.
        while not finished.wait(0.5):
            if link.status == RNS.Link.CLOSED:
                self._link = None
                return PageResult(request, error="Die Verbindung zur Node wurde getrennt.")
            if time.time() - last_activity[0] > REQUEST_IDLE_LIMIT:
                self._teardown()
                return PageResult(request, error="Zeitüberschreitung: Die Node antwortet nicht.")

        if outcome.get("failed"):
            self._teardown()
            return PageResult(request, error="Keine Antwort von der Node. Die Seite gibt es "
                                             "vielleicht nicht, oder die Verbindung ist zu langsam.")
        data = outcome.get("data")
        if isinstance(data, (bytes, bytearray)):
            return PageResult(request, bytes(data))
        if isinstance(data, str):
            return PageResult(request, data.encode("utf-8"))
        return PageResult(request, error="Die Node hat keine Seite geliefert.")

    def _teardown(self):
        link, self._link = self._link, None
        if link is not None:
            try:
                link.teardown()
            except Exception:
                pass


class _FetchError(Exception):
    pass
