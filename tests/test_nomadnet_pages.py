"""Tests for NomadNet page support: Micron parser, URLs, cache, colours."""

import threading
import types

import pytest
import RNS

from retchat import micron
from retchat.micron import Divider, Field, Line, Placeholder, Style, parse, parse_color
from retchat.nomadnet_pages import (
    DEFAULT_PATH,
    PageFetcher,
    PageRequest,
    get_cached,
    parse_url,
    store_cached,
    uncache,
)
from retchat.widgets.micron_view import contrast_ratio, readable_color

NODE = "ab" * 16


# --- Micron parser ---------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("f00", "#ff0000"),
    ("A1b2C3", "#a1b2c3"),
    ("g00", "#000000"),
    ("g50", "#808080"),
    ("g99", "#fcfcfc"),
    ("default", None),
    ("xyz", None),
    ("12", None),
])
def test_parse_color(value, expected):
    assert parse_color(value) == expected


def test_formatting_is_stateful_across_lines():
    doc = parse("`!`Ff00bold red\nstill bold red`!`f\nplain")
    spans = [b.parts[0] for b in doc.blocks]
    assert spans[0].style == Style(fg="#ff0000", bold=True)
    assert spans[1].style == Style(fg="#ff0000", bold=True)
    assert spans[2].style == Style()


def test_reset_and_alignment():
    doc = parse("`c`*centered\n``left again")
    assert doc.blocks[0].align == "center" and doc.blocks[0].parts[0].style.italic
    assert doc.blocks[1].align == "left" and doc.blocks[1].parts[0].style == Style()


def test_escapes():
    doc = parse("\\`! not bold\nprice \\`5")
    assert doc.blocks[0].text == "`! not bold"
    assert doc.blocks[1].text == "price `5"


def test_headings_sections_and_anchors():
    doc = parse(">Main Title\ntext\n>>Sub\nmore\n<back to top")
    heading, text, sub, more, top = doc.blocks
    assert (heading.heading, heading.depth) == (1, 1)
    assert text.depth == 1 and not text.heading
    assert (sub.heading, sub.depth, more.depth) == (2, 2, 2)
    assert top.depth == 0 and top.text == "back to top"
    assert doc.anchors["main-title"] == 0 and doc.anchors["sub"] == 2


def test_heading_formatting_does_not_leak():
    doc = parse("`!bold\n>Heading\nafter")
    assert not doc.blocks[1].parts[0].style.bold
    assert doc.blocks[2].parts[0].style.bold


def test_explicit_anchor():
    # The name ends at the first other character (kept, as in NomadNet)
    doc = parse("intro\n`:here Target line")
    assert doc.anchors["here"] == 1
    assert doc.blocks[1].text == " Target line"


def test_links():
    doc = parse("`[Label`:/page/x.mu`name|v=1] `[" + NODE + "]")
    link1, _space, link2 = doc.blocks[0].parts
    assert (link1.text, link1.link.url, link1.link.fields) == ("Label", ":/page/x.mu", ["name", "v=1"])
    assert (link2.text, link2.link.url) == (NODE, NODE)


def test_fields():
    doc = parse("`<name`Max> `<!16|pw`> `<8x3|msg`hi> `<?|opt|yes|*`Opt> `<^|col|red`Red>")
    fields = [p for p in doc.blocks[0].parts if isinstance(p, Field)]
    name, pw, msg, opt, col = fields
    assert (name.kind, name.name, name.value, name.width) == ("field", "name", "Max", 24)
    assert pw.masked and pw.width == 16
    assert (msg.width, msg.rows, msg.value) == (8, 3, "hi")
    assert (opt.kind, opt.value, opt.label, opt.checked) == ("checkbox", "yes", "Opt", True)
    assert (col.kind, col.value, col.checked) == ("radio", "red", False)


def test_heading_with_field_is_no_heading():
    doc = parse(">Name: `<name`>")
    assert doc.blocks[0].heading == 0


def test_literal_block():
    doc = parse("`=\n`!not bold `[x`y]\n\\`=\n`=\n`!bold")
    raw, escaped, after = doc.blocks
    assert raw.literal and raw.text == "`!not bold `[x`y]"
    assert escaped.text == "`="
    assert after.parts[0].style.bold


def test_dividers_comments_and_placeholders():
    doc = parse("#!c=60\n# comment\n-\n-=\n`(Alt text`w=10`/media/a.png)\n`{" + NODE + ":/page/p.mu`10}")
    assert doc.cache_time == 60
    div1, div2, image, partial = doc.blocks
    assert isinstance(div1, Divider) and div1.char == "\u2500"
    assert div2.char == "="
    assert isinstance(image, Placeholder) and image.text == "Alt text"
    assert isinstance(partial, Placeholder) and partial.kind == "partial"


def test_row_background():
    doc = parse("`B00fband\n`b\nnormal")  # a line with only commands produces no row
    assert doc.blocks[0].row_bg == "#0000ff"
    assert doc.blocks[1].text == "normal" and doc.blocks[1].row_bg is None


def test_table():
    doc = parse("`t\n| a | b |\n|---|---|\n| 1 | 2 |\n`t\nafter")
    texts = [b.text for b in doc.blocks]
    assert any("a" in t and "b" in t for t in texts)
    assert texts[-1] == "after"
    assert all(isinstance(b, Line) for b in doc.blocks)


def test_page_colors_and_cache_default():
    doc = parse("#!bg=222\n#!fg=ddd\ntext")
    assert (doc.page_bg, doc.page_fg) == ("#222222", "#dddddd")
    assert doc.cache_time == micron.DEFAULT_CACHE_TIME


def test_control_characters_are_removed():
    doc = parse("a\x1b[31mb")
    assert doc.blocks[0].text == "a[31mb"


# --- URLs ------------------------------------------------------------------------------

def test_parse_url_forms():
    node = bytes.fromhex(NODE)
    assert parse_url(NODE) == PageRequest(node, DEFAULT_PATH)
    assert parse_url(f"{NODE}:/page/a.mu") == PageRequest(node, "/page/a.mu")
    assert parse_url(f"nnn@{NODE}:/page/a.mu").path == "/page/a.mu"
    assert parse_url(":/page/b.mu", current_node=node) == PageRequest(node, "/page/b.mu")
    req = parse_url(f"{NODE}:/page/s.mu`q=mesh|page=2", data={"field_x": "1"})
    assert req.data == {"field_x": "1", "var_q": "mesh", "var_page": "2"}
    assert req.url == f"{NODE}:/page/s.mu`q=mesh|page=2"
    assert parse_url(f"{NODE}:/file/a.zip").is_file


@pytest.mark.parametrize("url", ["", "abc", ":/page/x.mu", f"{NODE}:page", "zz" * 16])
def test_parse_url_rejects(url):
    with pytest.raises(ValueError):
        parse_url(url)


# --- Cache ------------------------------------------------------------------------------

def test_cache_roundtrip_and_expiry(tmp_path):
    url = f"{NODE}:/page/index.mu"
    assert get_cached(str(tmp_path), url) is None
    store_cached(str(tmp_path), url, b">Hi", 60)
    assert get_cached(str(tmp_path), url) == b">Hi"
    # NomadNet's file name scheme: <sha256 of url>_<expiry timestamp>
    (name,) = [p.name for p in tmp_path.iterdir()]
    assert name.split("_")[0] == RNS.hexrep(RNS.Identity.full_hash(url.encode()), delimit=False)

    uncache(str(tmp_path), url)
    assert get_cached(str(tmp_path), url) is None

    store_cached(str(tmp_path), url, b"old", -1)  # cache time 0 / negative: not stored
    assert list(tmp_path.iterdir()) == []
    (tmp_path / (name.split("_")[0] + "_1.0")).write_bytes(b"expired")
    assert get_cached(str(tmp_path), url) is None
    assert list(tmp_path.iterdir()) == []


# --- Fetcher (own node, cache) ------------------------------------------------------------

def _fake_app(tmp_path, handlers):
    identity = RNS.Identity()
    node_dest = types.SimpleNamespace(hash=bytes.fromhex(NODE), request_handlers={
        RNS.Identity.truncated_hash(path.encode()): [path, fn, None, None, None] for path, fn in handlers.items()})
    return types.SimpleNamespace(cachepath=str(tmp_path), identity=identity,
                                 node=types.SimpleNamespace(destination=node_dest))


def _fetch(fetcher, request, use_cache=True):
    done = threading.Event()
    results = []
    fetcher.fetch(request, on_status=lambda *a: None,
                  on_done=lambda r: (results.append(r), done.set()), use_cache=use_cache)
    assert done.wait(5)
    return results[0]


def test_own_node_pages_are_served_locally(tmp_path):
    calls = []

    def serve_page(path, data, request_id, link_id, remote_identity, requested_at):
        calls.append((path, data))
        return b">Local page"

    def serve_default_index(path, data, request_id, remote_identity, requested_at):
        return b">Default"

    app = _fake_app(tmp_path, {"/page/a.mu": serve_page, "/page/index.mu": serve_default_index})
    fetcher = PageFetcher(app, dispatch=lambda fn, *args: fn(*args))
    node = bytes.fromhex(NODE)

    result = _fetch(fetcher, PageRequest(node, "/page/a.mu", {"var_x": "1"}))
    assert result.data == b">Local page" and calls == [("/page/a.mu", {"var_x": "1"})]
    assert _fetch(fetcher, PageRequest(node)).data == b">Default"
    assert "gibt es" in _fetch(fetcher, PageRequest(node, "/page/missing.mu")).error


def test_cached_pages_are_used(tmp_path):
    app = _fake_app(tmp_path, {})
    app.node = None
    fetcher = PageFetcher(app, dispatch=lambda fn, *args: fn(*args))
    request = PageRequest(bytes.fromhex("cd" * 16), "/page/c.mu")
    store_cached(str(tmp_path), request.url, b">Cached", 60)
    result = _fetch(fetcher, request)
    assert result.from_cache and result.data == b">Cached"


# --- Colours ----------------------------------------------------------------------------

@pytest.mark.parametrize("fg, bg", [
    ("#222222", "#222226"),  # dark text on dark theme
    ("#dddddd", "#fafafb"),  # light text on light theme
    ("#ffff00", "#fafafb"),  # yellow on white
    ("#0000ff", "#222226"),  # blue on dark
    ("#808080", "#808080"),  # same colour
])
def test_readable_color(fg, bg):
    fixed = readable_color(fg, bg)
    assert contrast_ratio(fixed, bg) >= 4.5


def test_readable_color_keeps_good_colors():
    assert readable_color("#000000", "#ffffff") == "#000000"
    assert readable_color("#ff8800", "#000000") == "#ff8800"


# --- Service: announces, nodes, operator ---------------------------------------------------

def _service_with_directory(stream):
    from retchat.reticulum_service import ReticulumService
    svc = ReticulumService.__new__(ReticulumService)
    svc._node_names = {}
    svc.app = types.SimpleNamespace(directory=types.SimpleNamespace(announce_stream=stream), node=None)
    return svc


def test_announces_are_classified_and_propagation_nodes_hidden():
    node, peer, pn = bytes.fromhex(NODE), bytes.fromhex("cd" * 16), bytes.fromhex("ef" * 16)
    svc = _service_with_directory([
        (10.0, node, b"My Node", "node"),
        (30.0, peer, b"Alice", "peer"),
        (20.0, pn, b"\x92\xc3\x01", "pn"),  # msgpack app data of a propagation node
        (5.0, peer, b"Alice (old)", "peer"),
    ])
    announces = svc.get_announces()
    assert [(a["display_name"], a["kind"]) for a in announces] == [("Alice", "peer"), ("My Node", "node")]
    assert svc.node_name(NODE) == "My Node"
    assert svc.is_node(NODE)


def test_operator_address_and_chat_address(monkeypatch):
    identity = RNS.Identity()
    node_hash = RNS.Destination.hash_from_name_and_identity("nomadnetwork.node", identity).hex()
    lxmf_hash = RNS.Destination.hash_from_name_and_identity("lxmf.delivery", identity).hex()
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h, *a, **k: identity))
    svc = _service_with_directory([])

    assert svc.is_node(node_hash)  # recognised by its identity, without an announce
    assert not svc.is_node(lxmf_hash)
    assert svc.operator_address(node_hash) == lxmf_hash
    assert svc.chat_address(node_hash) == lxmf_hash  # no empty "ghost" chat with the node
    assert svc.chat_address(lxmf_hash) == lxmf_hash


def test_node_name_from_remembered_announce(monkeypatch):
    monkeypatch.setattr(RNS.Identity, "recall_app_data", staticmethod(lambda h, *a, **k: "Catz Node".encode()))
    svc = _service_with_directory([])
    assert svc.node_name(NODE) == "Catz Node"


# --- nomadnetwork:// links in messages ---------------------------------------------------

from retchat.nomadnet_pages import find_urls  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    (f"nomadnetwork://{NODE}", [f"nomadnetwork://{NODE}"]),
    (f"Schau mal: nomadnetwork://{NODE}:/page/news.mu.", [f"nomadnetwork://{NODE}:/page/news.mu"]),
    (f"(nomadnetwork://{NODE}:/page/a.mu)", [f"nomadnetwork://{NODE}:/page/a.mu"]),
    (f"nomadnetwork://{NODE}:/page/s.mu`q=mesh, danach", [f"nomadnetwork://{NODE}:/page/s.mu`q=mesh"]),
    (f"a nomadnetwork://{NODE} b NOMADNETWORK://{NODE.upper()}:/page/x.mu",
     [f"nomadnetwork://{NODE}", f"NOMADNETWORK://{NODE.upper()}:/page/x.mu"]),
    ("nomadnetwork://abc", []),  # not a hash
    (f"https://{NODE}", []),
])
def test_find_urls(text, expected):
    found = find_urls(text)
    assert [url for _s, _e, url in found] == expected
    assert all(text[s:e] == url for s, e, url in found)


def test_parse_url_accepts_scheme():
    req = parse_url(f"nomadnetwork://{NODE}:/page/news.mu`id=4")
    assert (req.node_hex, req.path, req.data) == (NODE, "/page/news.mu", {"var_id": "4"})
    assert parse_url(f"NomadNetwork://{NODE}").path == DEFAULT_PATH


def test_linkify_escapes_and_links():
    from retchat.widgets.message_bubble import linkify
    assert linkify("kein Link <b>") is None
    markup = linkify(f"<Seite> nomadnetwork://{NODE}:/page/a&b.mu!")
    assert markup.startswith("&lt;Seite&gt; <a href=")
    assert f'href="nomadnetwork://{NODE}:/page/a&amp;b.mu"' in markup
    assert markup.endswith("</a>!")
