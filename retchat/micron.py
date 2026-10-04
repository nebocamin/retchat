"""Parser for Micron, the markup language of NomadNet pages.

Ported from NomadNet's MicronParser (nomadnet/ui/textui/MicronParser.py,
Copyright (c) Mark Qvist, GNU GPL v3) and modified for Retchat, 2026-09-29
and later (see the git history): instead of urwid widgets it produces a
small, toolkit independent document model that the GTK renderer
(retchat.widgets.micron_view) displays. As a work based on NomadNet, this
file is licensed under the GNU GPL v3.

Micron is line based and stateful: formatting, colours and alignment set on
one line stay active on the following lines until they are reset.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

try:
    from nomadnet.util import STRIP_CONTROL_RE
except Exception:  # pragma: no cover - nomadnet is a hard dependency
    STRIP_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+")

SECTION_INDENT = 2  # characters per section level
MAX_TABLE_WIDTH = 100
DEFAULT_FIELD_WIDTH = 24
DEFAULT_CACHE_TIME = 12 * 60 * 60


@dataclass(frozen=True)
class Style:
    """fg/bg are "#rrggbb" or None for the default (theme) colour."""
    fg: Optional[str] = None
    bg: Optional[str] = None
    bold: bool = False
    italic: bool = False
    underline: bool = False


@dataclass
class Link:
    url: str
    # Names of form fields to submit, "*" for all, or "key=value" variables
    fields: List[str] = field(default_factory=list)


@dataclass
class Span:
    text: str
    style: Style
    link: Optional[Link] = None


@dataclass
class Field:
    kind: str  # "field", "checkbox" or "radio"
    name: str
    style: Style
    value: str = ""  # initial text (field) or submitted value (checkbox/radio)
    label: str = ""  # checkbox/radio label
    width: int = DEFAULT_FIELD_WIDTH
    rows: int = 1
    masked: bool = False
    checked: bool = False


Part = Union[Span, Field]


@dataclass
class Line:
    parts: List[Part]
    align: str = "left"  # "left", "center" or "right"
    depth: int = 0  # section depth, indents the line by (depth-1)*SECTION_INDENT
    heading: int = 0  # heading level (1 = ">"), 0 for normal lines
    literal: bool = False  # from a `= literal block: no wrapping, no markup
    row_bg: Optional[str] = None  # background colour active at the end of the line fills the row
    anchors: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(p.text if isinstance(p, Span) else "" for p in self.parts)


@dataclass
class Divider:
    char: str = "\u2500"
    depth: int = 0
    anchors: List[str] = field(default_factory=list)


@dataclass
class Placeholder:
    """Content that isn't supported yet (images, partials)."""
    kind: str
    text: str
    depth: int = 0
    anchors: List[str] = field(default_factory=list)


Block = Union[Line, Divider, Placeholder]


@dataclass
class Document:
    blocks: List[Block]
    anchors: Dict[str, int]  # anchor name -> block index
    cache_time: int = DEFAULT_CACHE_TIME  # seconds, 0: don't cache
    page_fg: Optional[str] = None  # #!fg / #!bg page colours (informative)
    page_bg: Optional[str] = None


# --- Colours ------------------------------------------------------------------------

_HEX = set("0123456789abcdefABCDEF")


def parse_color(value: Optional[str]) -> Optional[str]:
    """Micron colour ("rgb", "rrggbb" or grey level "gNN") as "#rrggbb"."""
    if not value or value == "default":
        return None
    if len(value) == 6 and all(c in _HEX for c in value):
        return "#" + value.lower()
    if len(value) == 3:
        if value[0] == "g" and value[1:].isdigit():
            level = round(min(int(value[1:]), 100) / 100 * 255)  # percent
            return "#" + f"{level:02x}" * 3
        if all(c in _HEX for c in value):
            return "#" + "".join(c * 2 for c in value.lower())
    return None


def _page_color(markup: str, directive: str) -> Optional[str]:
    pos = markup.find(directive)
    if pos < 0:
        return None
    end = markup.find("\n", pos)
    value = markup[pos + len(directive):end if end >= 0 else len(markup)].strip()
    return parse_color(value) if len(value) in (3, 6) else None


def cache_time_of(markup: str) -> int:
    """Cache lifetime from a "#!c=<seconds>" header on the first line."""
    if markup.startswith("#!c="):
        end = markup.find("\n")
        try:
            return max(0, int(markup[4:end if end >= 0 else len(markup)].strip()))
        except ValueError:
            pass
    return DEFAULT_CACHE_TIME


# --- Anchors ------------------------------------------------------------------------

_MICRON_STRIP_RE = re.compile(
    r"`[FB]T[0-9a-fA-F]{6}"
    r"|`[FB][0-9a-fA-F]{3}"
    r"|`:[A-Za-z0-9_\-]*"
    r"|`[!*_=fbacrl`<>{]"
)


def slugify(text: str) -> str:
    """Automatic anchor name of a heading (as NomadNet computes it)."""
    stripped = _MICRON_STRIP_RE.sub("", text or "")
    return re.sub(r"[^A-Za-z0-9]+", "-", stripped).strip("-").lower()


# --- Parser -------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.literal = False
        self.depth = 0
        self.fg: Optional[str] = None
        self.bg: Optional[str] = None
        self.bold = False
        self.italic = False
        self.underline = False
        self.align = "left"
        self.pending_anchors: List[str] = []
        self.table: Optional[List[str]] = None
        self.table_align: Optional[str] = None
        self.table_width: Optional[int] = None

    def style(self) -> Style:
        return Style(parse_color(self.fg), parse_color(self.bg), self.bold, self.italic, self.underline)

    def reset(self):
        self.bold = self.italic = self.underline = False
        self.fg = self.bg = None
        self.align = "left"


def parse(markup: str) -> Document:
    """Parse a Micron page into a Document."""
    markup = STRIP_CONTROL_RE.sub("", markup.replace("\r\n", "\n"))
    state = _State()
    blocks: List[Block] = []
    anchors: Dict[str, int] = {}

    for raw in markup.split("\n"):
        for block in _parse_line(raw, state):
            names = getattr(block, "anchors")
            names.extend(state.pending_anchors)
            state.pending_anchors = []
            for name in names:
                anchors.setdefault(name, len(blocks))
            blocks.append(block)

    if state.table is not None:  # unterminated table: show its lines
        for raw in state.table:
            blocks.append(Line([Span(raw, state.style())], depth=state.depth))

    return Document(
        blocks=blocks,
        anchors=anchors,
        cache_time=cache_time_of(markup),
        page_fg=_page_color(markup, "#!fg="),
        page_bg=_page_color(markup, "#!bg="),
    )


def _indentable(block: Block, state: _State) -> Block:
    if isinstance(block, (Line, Divider, Placeholder)):
        block.depth = state.depth
    return block


def _parse_line(line: str, state: _State) -> List[Block]:
    if line == "":
        # Also inside tables (as in NomadNet); an active background fills the row.
        return [Line([], align=state.align, depth=state.depth, literal=state.literal,
                     row_bg=parse_color(state.bg))]

    if line == "`=":
        state.literal = not state.literal
        return []

    if state.literal:
        if line == "\\`=":
            line = "`="
        style = state.style()
        return [Line([Span(line, style)], align=state.align, depth=state.depth, literal=True,
                     row_bg=style.bg)]

    pre_escape = False
    first = line[0]

    # Collapsible headings (`+> open, `-> collapsed) are shown as normal headings.
    if (line.startswith("`+") or line.startswith("`-")) and line[2:3] == ">":
        line = line[2:]
        first = line[0]

    if first == ">" and "`<" in line:  # lines with fields can't be headings
        line = line.lstrip(">")
        if not line:
            return []
        first = line[0]

    if first == "\\":
        line = line[1:]
        pre_escape = True
    elif first == "#":
        return []  # comment or page directive

    if not pre_escape and line.startswith("`t"):
        return _table_toggle(line[2:], state)
    if state.table is not None:
        state.table.append(line)
        return []

    if not pre_escape:
        if line.startswith("`{"):
            return [Placeholder("partial", "Dynamischer Seitenteil", depth=state.depth)]
        if line.startswith("`("):
            return [_image_placeholder(line[2:], state)]
        if first == "<":
            state.depth = 0
            return _parse_line(line[1:], state) if len(line) > 1 else []
        if first == ">":
            return _heading(line, state)
        if first == "-":
            char = line[1] if len(line) == 2 and ord(line[1]) >= 32 else "\u2500"
            return [Divider(char, depth=state.depth)]

    parts = _make_output(line, state, pre_escape)
    if not parts:
        return []
    return [Line(parts, align=state.align, depth=state.depth, row_bg=parse_color(state.bg))]


def _heading(line: str, state: _State) -> List[Block]:
    level = len(line) - len(line.lstrip(">"))
    state.depth = level
    text = line[level:]
    if not text:
        return []
    # Headings have their own look; the current formatting is restored after it.
    saved = (state.fg, state.bg, state.bold, state.italic, state.underline)
    state.fg = state.bg = None
    state.bold = state.italic = state.underline = False
    parts = _make_output(text, state, False)
    state.fg, state.bg, state.bold, state.italic, state.underline = saved
    if not parts:
        return []
    slug = slugify(text)
    return [Line(parts, align=state.align, depth=level, heading=level, anchors=[slug] if slug else [])]


def _image_placeholder(line: str, state: _State) -> Placeholder:
    end = line.rfind(")")
    alt = line[:end].split("`")[0].strip() if end > 0 else ""
    return Placeholder("image", alt or "Bild", depth=state.depth)


def _table_toggle(spec: str, state: _State) -> List[Block]:
    if state.table is None:
        state.table_align = spec[0] if spec[:1] in ("l", "c", "r") else None
        rest = spec[1:] if state.table_align else spec
        try:
            state.table_width = int(rest) if rest else None
        except ValueError:
            state.table_width = None
        state.table = []
        return []

    rows, state.table = state.table, None
    lines = rows
    if len(rows) >= 2:
        try:
            from RNS.Utilities.rngit.util import MarkdownToMicron
            converter = MarkdownToMicron(max_width=state.table_width or MAX_TABLE_WIDTH)
            lines = converter.format_table_raw(rows, align=state.table_align)
        except Exception:
            lines = rows
    blocks: List[Block] = []
    for row in lines:
        blocks.extend(_parse_line(row, state))
    return blocks


def _make_output(line: str, state: _State, pre_escape: bool) -> List[Part]:
    """Split a line into styled spans, links and fields, updating the state."""
    output: List[Part] = []
    part = ""
    escape = pre_escape
    i = 0
    n = len(line)

    def flush():
        nonlocal part
        if part:
            output.append(Span(part, state.style()))
            part = ""

    while i < n:
        c = line[i]
        if c == "\\" and not escape:
            escape = True
            i += 1
            continue
        if c != "`" or escape:
            part += c
            escape = False
            i += 1
            continue

        # Formatting command after a backtick
        flush()
        i += 1
        if i >= n:
            break
        c = line[i]
        if c == "_":
            state.underline = not state.underline
        elif c == "!":
            state.bold = not state.bold
        elif c == "*":
            state.italic = not state.italic
        elif c in "FB":
            if line[i + 1:i + 2] == "T" and i + 8 <= n:
                color, skip = line[i + 2:i + 8], 7
            elif i + 4 <= n:
                color, skip = line[i + 1:i + 4], 3
            else:
                color, skip = None, 0
            if color is not None:
                if c == "F":
                    state.fg = color
                else:
                    state.bg = color
            i += skip
        elif c == "f":
            state.fg = None
        elif c == "b":
            state.bg = None
        elif c == "`":
            state.reset()
        elif c == "c":
            state.align = "center"
        elif c == "l":
            state.align = "left"
        elif c == "r":
            state.align = "right"
        elif c == "a":
            state.align = "left"
        elif c == ":":
            end = i + 1
            while end < n and (line[end].isalnum() or line[end] in "_-"):
                end += 1
            if end > i + 1:
                state.pending_anchors.append(line[i + 1:end])
            i = end - 1
        elif c == "<":
            parsed = _parse_field(line, i, state)
            if parsed is not None:
                fld, i = parsed
                output.append(fld)
        elif c == "[":
            end = line.find("]", i)
            if end != -1:
                link = _parse_link(line[i + 1:end], state)
                if link is not None:
                    output.append(link)
                i = end
        i += 1

    flush()
    return output


def _parse_link(data: str, state: _State) -> Optional[Span]:
    components = data.split("`")
    if len(components) == 1:
        label, url, fields = "", data, ""
    elif len(components) == 2:
        label, url, fields = components[0], components[1], ""
    elif len(components) == 3:
        label, url, fields = components
    else:
        return None
    if not url:
        return None
    return Span(label or url, state.style(), Link(url, fields.split("|") if fields else []))


def _parse_field(line: str, i: int, state: _State):
    """Field starting at line[i] == "<"; returns (Field, index of the closing ">")."""
    backtick = line.find("`", i + 1)
    if backtick == -1:
        return None
    end = line.find(">", backtick)
    if end == -1:
        return None
    content = line[i + 1:backtick]
    data = line[backtick + 1:end]
    style = state.style()

    if "|" not in content:
        return Field("field", content, style, value=data), end

    components = content.split("|")
    flags, name = components[0], components[1]
    value = components[2] if len(components) > 2 else ""
    checked = len(components) > 3 and components[3] == "*"

    kind, masked = "field", False
    if "^" in flags:
        kind, flags = "radio", flags.replace("^", "")
    elif "?" in flags:
        kind, flags = "checkbox", flags.replace("?", "")
    elif "!" in flags:
        masked, flags = True, flags.replace("!", "")

    width, rows = DEFAULT_FIELD_WIDTH, 1
    if flags:
        width_flag, _, rows_flag = flags.partition("x")
        try:
            width = min(int(width_flag), 256)
        except ValueError:
            pass
        if rows_flag:
            try:
                rows = max(1, min(int(rows_flag), 256))
            except ValueError:
                pass

    if kind in ("checkbox", "radio"):
        return Field(kind, name, style, value=value or data, label=data, checked=checked), end
    return Field("field", name, style, value=data, width=width, rows=rows, masked=masked), end
