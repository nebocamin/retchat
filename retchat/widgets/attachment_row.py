"""File attachment row shown inside a message bubble, plus file helpers."""

import hashlib
import os
import shutil

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk, Pango

# Content types that must not be opened directly: received files come from
# anyone on the mesh. They can still be saved.
EXECUTABLE_TYPES = (
    "application/x-executable",
    "application/x-sharedlib",
    "application/x-shellscript",
    "application/x-desktop",
    "application/x-msdownload",
    "application/x-ms-dos-executable",
    "application/x-msi",
    "application/x-bat",
    "application/vnd.appimage",
    "application/x-java-archive",
    "application/x-perl",
    "application/x-ruby",
    "text/x-python",
)
_EXECUTABLE_MAGIC = (b"\x7fELF", b"MZ", b"#!")


def _head(path: str, size: int = 512) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def content_type(name: str, path: str) -> str:
    # No data (unreadable file) must be None: empty data means "empty file" to GLib.
    ctype, _uncertain = Gio.content_type_guess(name, _head(path) or None)
    return ctype


def is_executable(name: str, path: str) -> bool:
    if _head(path, 4).startswith(_EXECUTABLE_MAGIC):
        return True
    ctype = content_type(name, path)
    return any(Gio.content_type_is_a(ctype, t) for t in EXECUTABLE_TYPES)


def file_icon(name: str, path: str) -> Gio.Icon:
    # Generic Adwaita icon per kind (document, audio, archive, …): specific
    # MIME icons may come from other installed themes and look out of place.
    generic = Gio.content_type_get_generic_icon_name(content_type(name, path)) or "text-x-generic"
    return Gio.ThemedIcon.new_from_names([f"{generic}-symbolic", "text-x-generic-symbolic"])


def open_copy_dir() -> str:
    return os.path.join(GLib.get_user_cache_dir(), "retchat", "open")


def named_copy(path: str, name: str) -> str:
    """A copy of ``path`` carrying its real file name, for opening in other apps.

    NomadNet stores attachments as ``file_0``; many apps rely on the name or
    extension to detect the type. Uses a hard link where possible.
    """
    target_dir = os.path.join(open_copy_dir(), hashlib.sha256(path.encode()).hexdigest()[:16])
    target = os.path.join(target_dir, os.path.basename(name) or "attachment")
    if not os.path.exists(target):
        os.makedirs(target_dir, exist_ok=True)
        try:
            os.link(path, target)
        except OSError:
            shutil.copyfile(path, target)
    return target


def purge_open_copies():
    """Remove copies created for opening files (called at start-up)."""
    shutil.rmtree(open_copy_dir(), ignore_errors=True)


class AttachmentRow(Gtk.Box):
    """Icon, name and size of an attachment with open/save buttons.

    The buttons use the ``chat.open-attachment`` / ``chat.save-attachment``
    actions (target: path and name) instead of Python signal handlers, so
    rows can be created and dropped freely without keeping each other alive.
    """

    def __init__(self, path: str, name: str, size: int):
        super().__init__(spacing=10)
        self.add_css_class("attachment-row")
        self.path = path
        self.name = name
        available = os.path.isfile(path)
        target = GLib.Variant("(ss)", (path, name))

        icon = Gtk.Image(gicon=file_icon(name, path), pixel_size=32, valign=Gtk.Align.CENTER)
        icon.add_css_class("attachment-icon")
        self.append(icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        name_label = Gtk.Label(label=name, xalign=0.0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                               max_width_chars=28, tooltip_text=name)
        name_label.add_css_class("attachment-name")
        text.append(name_label)
        detail = GLib.format_size(size) if available else "Datei nicht verfügbar"
        self.detail_label = Gtk.Label(label=detail, xalign=0.0)
        self.detail_label.add_css_class("attachment-size")
        text.append(self.detail_label)
        self.append(text)

        self.open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Öffnen",
                                   valign=Gtk.Align.CENTER, action_name="chat.open-attachment",
                                   action_target=target)
        self.open_btn.add_css_class("flat")
        self.open_btn.add_css_class("circular")
        self.open_btn.set_visible(available and not is_executable(name, path))
        self.append(self.open_btn)

        self.save_btn = Gtk.Button(icon_name="document-save-as-symbolic", tooltip_text="Speichern unter…",
                                   valign=Gtk.Align.CENTER, action_name="chat.save-attachment",
                                   action_target=target)
        self.save_btn.add_css_class("flat")
        self.save_btn.add_css_class("circular")
        self.save_btn.set_visible(available)
        self.append(self.save_btn)
