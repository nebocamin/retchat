"""Reticulum and NomadNet LXMF Service integration for Retchat."""

import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import gi
gi.require_version('GLib', '2.0')
from gi.repository import GLib

import msgpack
import LXMF
import nomadnet
from nomadnet import Conversation
from nomadnet.Conversation import ConversationMessage
import RNS

from retchat.database import Database

# Message states mapped for UI
STATE_SENDING = 0
STATE_SENT = 1
STATE_DELIVERED = 2
STATE_FAILED = 3

def _release(m: ConversationMessage):
    """Drop a fully loaded LXMF message (content, fields, image data) again.

    NomadNet's ConversationMessage.load() keeps the whole LXMessage in
    ``m.lxm`` until unload(); conversations are cached for the app's
    lifetime, so every message Retchat ever read would stay in memory.
    Hash, state, content, title and timestamp stay available afterwards
    from NomadNet's per-message cache and index.
    """
    if getattr(m, "lxm", None) is not None:
        m.unload()


def _source_hash(m: ConversationMessage) -> Optional[bytes]:
    # NomadNet has no public getter; the value is restored from its index,
    # so this usually avoids reading the message file at all.
    cached = getattr(m, "_cached_source_hash", None)
    if cached is not None:
        return cached
    if not m.loaded:
        m.load()
    return m.lxm.source_hash if m.lxm is not None else None


# LXMF receivers accept messages up to 1000 KB by default (LXMRouter.DELIVERY_LIMIT),
# the whole packed message counts; leave room for text and overhead.
MAX_ATTACHMENT_SIZE = 900_000
# Propagation nodes accept 256 KB per message by default, larger messages
# can only be delivered directly.
PROPAGATION_SIZE_LIMIT = 256_000


def is_sendable_image(path: str) -> bool:
    """Whether ``path`` is sent as (downscaled) image instead of as a file."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.format in ("JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF")
    except Exception:
        return False


def _check_attachment_size(size: int):
    if size > MAX_ATTACHMENT_SIZE:
        raise ValueError(f"Datei zu groß ({size / 1000:.0f} KB, höchstens {MAX_ATTACHMENT_SIZE // 1000} KB)")


# Truncated destination hash (16 bytes) and full LXMF message hash (32 bytes) as hex
_HEX_DEST_RE = re.compile(r"[0-9a-f]{32}")
_HEX_MSG_RE = re.compile(r"[0-9a-f]{64}")


def map_lxmf_state_to_ui(lxmf_state: int, is_outgoing: bool = False) -> int:
    """Map NomadNet / LXMF message state enum to Retchat UI states."""
    if not is_outgoing:
        return STATE_DELIVERED
    if lxmf_state in (LXMF.LXMessage.GENERATING, LXMF.LXMessage.OUTBOUND, LXMF.LXMessage.SENDING):
        return STATE_SENDING
    elif lxmf_state == LXMF.LXMessage.SENT:
        return STATE_SENT
    elif lxmf_state == LXMF.LXMessage.DELIVERED:
        return STATE_DELIVERED
    elif lxmf_state in (LXMF.LXMessage.FAILED, LXMF.LXMessage.REJECTED, LXMF.LXMessage.CANCELLED):
        return STATE_FAILED
    return STATE_SENDING


class _QuietUI:
    """Replacement for the Nomad Network curses/urwid daemon UI.

    The default NomadNet UI runs an endless terminal event loop which would block
    the calling thread forever. This quiet object lets the embedding GTK application
    maintain control of the main loop.
    """
    restore_ixon = None
    restore_palette = None

    def __init__(self):
        from nomadnet import NomadNetworkApp
        self.app = NomadNetworkApp.get_shared_instance()
        self.app.ui = self
        self.glyphs = {}
        self.screen = None
        RNS.log("Retchat: embedding NomadNetworkApp (quiet UI)",
                RNS.LOG_INFO, _override_destination=True)


def install_quiet_ui():
    """Monkeypatch nomadnet.ui.spawn so NomadNet starts quietly without blocking."""
    if getattr(nomadnet.ui, "_retchat_quiet_installed", False):
        return
    _orig_spawn = nomadnet.ui.spawn

    def _quiet_spawn(uimode):
        return _QuietUI()

    nomadnet.ui.spawn = _quiet_spawn
    nomadnet.ui._retchat_quiet_installed = True
    return _orig_spawn


_outbound_state_listener: Optional[Callable[[LXMF.LXMessage], None]] = None


def install_delivery_hook(listener: Callable[[LXMF.LXMessage], None]):
    """Report state changes of sent messages to ``listener``.

    NomadNet registers Conversation.message_notification as delivery and
    failure callback on every LXMessage it sends (the object the router
    holds). The hook runs after NomadNet's handling, so a failed direct
    delivery that NomadNet retries via the propagation node is reported
    as sending again, not as failed. Called from Reticulum threads.
    """
    global _outbound_state_listener
    _outbound_state_listener = listener
    if getattr(Conversation.message_notification, "_retchat_hook", False):
        return
    original = Conversation.message_notification

    def message_notification(conversation, message):
        original(conversation, message)
        if _outbound_state_listener is not None:
            try:
                _outbound_state_listener(message)
            except Exception as e:
                RNS.log(f"Retchat: Error reporting message state: {e}", RNS.LOG_ERROR)

    message_notification._retchat_hook = True
    Conversation.message_notification = message_notification


class _RetchatApp(nomadnet.NomadNetworkApp):
    """NomadNetworkApp subclass that intercepts delivery events for Retchat."""

    def __init__(self, service, configdir=None, rnsconfigdir=None):
        self._service = service
        super().__init__(configdir=configdir, rnsconfigdir=rnsconfigdir, daemon=True)

    def lxmf_delivery(self, message):
        super().lxmf_delivery(message)
        try:
            self._service._on_inbound_lxmessage(message)
        except Exception as e:
            RNS.log(f"Retchat: Error in lxmf_delivery handler: {e}", RNS.LOG_ERROR)


class ReticulumService:
    def __init__(self, db: Database, configdir=None, rnsconfigdir=None):
        self.db = db
        self._configdir = configdir
        self._rnsconfigdir = rnsconfigdir

        # Callbacks for UI updates
        self._message_received_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._message_state_callbacks: List[Callable[[str, int], None]] = []
        self._message_id_callbacks: List[Callable[[str, str], None]] = []
        self._announce_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._path_resolved_callbacks: List[Callable[[str, int], None]] = []
        self._conversations_changed_callbacks: List[Callable[[], None]] = []

        # Ensure ~/.reticulum/config has TCP enabled as default, and enable_transport=False
        self._ensure_tcp_default_config()

        # Install quiet UI monkeypatch before creating NomadNetworkApp
        install_quiet_ui()

        Conversation.created_callback = self._on_conversations_changed_nomadnet
        install_delivery_hook(self._on_outbound_state)
        self.app = _RetchatApp(self, configdir=self._configdir, rnsconfigdir=self._rnsconfigdir)

        self._conv_cache: Dict[str, Conversation] = {}
        # dest hash -> queued (content, attachment path, temporary message hash)
        self._pending: Dict[str, List[Tuple[str, Optional[str], str]]] = {}

        # Hook announce logger on app.directory to dispatch announces without extra overhead
        self._hook_directory_announces()

        # Periodic background worker for flushing pending messages
        self._poll_stop = False
        self._poll_thread = threading.Thread(target=self._background_worker, daemon=True, name="Retchat-Worker")
        self._poll_thread.start()

        RNS.log(
            f"Retchat: ReticulumService initialised with NomadNet backend. "
            f"Identity: {self.identity_hex}, Destination: {self.delivery_destination_hex}",
            RNS.LOG_NOTICE
        )

    def _ensure_tcp_default_config(self):
        """Ensure ~/.reticulum/config exists with TCP enabled and transport disabled for mobile phones."""
        try:
            cfg_dir = os.path.expanduser("~/.reticulum")
            if os.path.isdir(os.path.expanduser("~/.config/reticulum")) and os.path.isfile(os.path.expanduser("~/.config/reticulum/config")):
                cfg_dir = os.path.expanduser("~/.config/reticulum")
            os.makedirs(cfg_dir, exist_ok=True)
            cfg_path = os.path.join(cfg_dir, "config")

            default_host = "sideband.connect.reticulum.network"
            default_port = "7822"

            from RNS.vendor.configobj import ConfigObj

            if not os.path.exists(cfg_path):
                cfg = ConfigObj()
                cfg.filename = cfg_path
                cfg["reticulum"] = {
                    "enable_transport": "False",
                    "share_instance": "Yes",
                    "instance_name": "default"
                }
                cfg["logging"] = {
                    "loglevel": "4"
                }
                cfg["interfaces"] = {
                    "Default TCP Client": {
                        "type": "TCPClientInterface",
                        "enabled": "yes",
                        "target_host": default_host,
                        "target_port": default_port
                    },
                    "Default Interface": {
                        "type": "AutoInterface",
                        "enabled": "no"
                    }
                }
                cfg.write()
                RNS.log("Retchat: Created mobile-friendly default Reticulum config", RNS.LOG_NOTICE)
            else:
                cfg = ConfigObj(cfg_path)
                modified = False
                if "reticulum" in cfg:
                    if str(cfg["reticulum"].get("enable_transport", "")).lower() in ("true", "yes", "1"):
                        cfg["reticulum"]["enable_transport"] = "False"
                        modified = True
                        RNS.log("Retchat: Disabled enable_transport in existing Reticulum config to conserve CPU/battery", RNS.LOG_NOTICE)
                interfaces = cfg.get("interfaces", {})
                has_tcp = False
                for name, iface in interfaces.items():
                    if isinstance(iface, dict) and iface.get("type") in ("TCPClientInterface", "TCPInterface"):
                        en = str(iface.get("enabled", iface.get("interface_enabled", "true"))).lower()
                        if en in ("true", "yes", "1"):
                            has_tcp = True
                            break
                if not has_tcp:
                    if "interfaces" not in cfg:
                        cfg["interfaces"] = {}
                    cfg["interfaces"]["Default TCP Client"] = {
                        "type": "TCPClientInterface",
                        "enabled": "yes",
                        "target_host": default_host,
                        "target_port": default_port
                    }
                    modified = True
                    RNS.log("Retchat: Added default TCP Client interface to existing config", RNS.LOG_NOTICE)
                if modified:
                    cfg.write()
        except Exception as e:
            RNS.log(f"Retchat: Error initializing default TCP config: {e}", RNS.LOG_WARNING)

    def get_tcp_settings(self) -> Dict[str, Any]:
        """Read TCP and AutoInterface settings from ~/.reticulum/config."""
        cfg_dir = os.path.expanduser("~/.reticulum")
        if os.path.isdir(os.path.expanduser("~/.config/reticulum")) and os.path.isfile(os.path.expanduser("~/.config/reticulum/config")):
            cfg_dir = os.path.expanduser("~/.config/reticulum")
        cfg_path = os.path.join(cfg_dir, "config")

        default_host = "sideband.connect.reticulum.network"
        default_port = "7822"
        disable_auto = False

        if os.path.exists(cfg_path):
            try:
                from RNS.vendor.configobj import ConfigObj
                cfg = ConfigObj(cfg_path)
                interfaces = cfg.get("interfaces", {})
                for name, iface in interfaces.items():
                    if isinstance(iface, dict) and iface.get("type") in ("TCPClientInterface", "TCPInterface"):
                        host = iface.get("target_host")
                        port = iface.get("target_port")
                        if host:
                            default_host = str(host)
                        if port:
                            default_port = str(port)
                        break
                auto_iface = interfaces.get("Default Interface")
                if isinstance(auto_iface, dict):
                    en = str(auto_iface.get("enabled", auto_iface.get("interface_enabled", "true"))).lower()
                    if en in ("false", "no", "0"):
                        disable_auto = True
            except Exception:
                pass

        return {
            "host": default_host,
            "port": default_port,
            "disable_auto": disable_auto,
            "config_path": cfg_path
        }

    def save_tcp_settings(self, host: str, port: str, disable_auto: bool) -> str:
        """Update ~/.reticulum/config with TCP interface and AutoInterface preferences."""
        cfg_dir = os.path.expanduser("~/.reticulum")
        if os.path.isdir(os.path.expanduser("~/.config/reticulum")) and os.path.isfile(os.path.expanduser("~/.config/reticulum/config")):
            cfg_dir = os.path.expanduser("~/.config/reticulum")
        cfg_path = os.path.join(cfg_dir, "config")

        from RNS.vendor.configobj import ConfigObj
        cfg = ConfigObj(cfg_path) if os.path.exists(cfg_path) else ConfigObj()
        cfg.filename = cfg_path

        if "reticulum" not in cfg:
            cfg["reticulum"] = {
                "enable_transport": "False",
                "share_instance": "Yes",
                "instance_name": "default"
            }
        else:
            cfg["reticulum"]["enable_transport"] = "False"

        if "interfaces" not in cfg:
            cfg["interfaces"] = {}

        tcp_key = None
        for name, iface in cfg["interfaces"].items():
            if isinstance(iface, dict) and iface.get("type") in ("TCPClientInterface", "TCPInterface"):
                tcp_key = name
                break
        if not tcp_key:
            tcp_key = "Default TCP Client"
            cfg["interfaces"][tcp_key] = {"type": "TCPClientInterface"}

        cfg["interfaces"][tcp_key]["type"] = "TCPClientInterface"
        cfg["interfaces"][tcp_key]["enabled"] = "yes"
        cfg["interfaces"][tcp_key]["target_host"] = host.strip()
        cfg["interfaces"][tcp_key]["target_port"] = port.strip()

        if "Default Interface" in cfg["interfaces"]:
            cfg["interfaces"]["Default Interface"]["enabled"] = "no" if disable_auto else "yes"
        elif disable_auto:
            cfg["interfaces"]["Default Interface"] = {
                "type": "AutoInterface",
                "enabled": "no"
            }

        cfg.write()
        return cfg_path

    # --- Directory Announce Hooking ---
    def _hook_directory_announces(self):
        if not hasattr(self.app, "directory"):
            return
        orig_lxmf = self.app.directory.lxmf_announce_received
        def _hooked_lxmf(source_hash, app_data):
            orig_lxmf(source_hash, app_data)
            self._dispatch_directory_announce(source_hash, app_data, "peer")
        self.app.directory.lxmf_announce_received = _hooked_lxmf

        orig_node = self.app.directory.node_announce_received
        def _hooked_node(source_hash, app_data, associated_peer):
            orig_node(source_hash, app_data, associated_peer)
            self._dispatch_directory_announce(source_hash, app_data, "node")
        self.app.directory.node_announce_received = _hooked_node

    def _dispatch_directory_announce(self, source_hash, app_data, kind: str):
        try:
            dest_hex = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash)
            dest_hex = dest_hex.lower()
            name = None
            if app_data:
                if isinstance(app_data, bytes):
                    name = app_data.decode("utf-8", errors="replace")
                else:
                    name = str(app_data)
            hops = RNS.Transport.hops_to(source_hash if isinstance(source_hash, bytes) else bytes.fromhex(dest_hex))
            if hops == RNS.Transport.PATHFINDER_M:
                hops = 0

            data = {
                "destination_hash": dest_hex,
                "display_name": name,
                "hops": hops,
                "aspect": f"nomadnet.{kind}",
                "receiving_interface": "",
                "last_seen": time.time()
            }
            GLib.idle_add(self._notify_announce, data)
        except Exception as e:
            RNS.log(f"Retchat: Error dispatching announce: {e}", RNS.LOG_DEBUG)

    # --- Identity & Profile ---
    @property
    def identity(self) -> RNS.Identity:
        return self.app.identity

    @property
    def delivery_dest(self) -> RNS.Destination:
        return self.app.lxmf_destination

    @property
    def identity_hex(self) -> str:
        return self.app.identity.hash.hex()

    @property
    def delivery_destination_hex(self) -> str:
        return self.app.lxmf_destination.hash.hex()

    @property
    def display_name(self) -> str:
        name = self.app.get_display_name()
        return name if name else "Retchat"

    def set_display_name(self, new_name: str, announce_now: bool = True):
        new_name = new_name.strip()
        if not new_name:
            return
        try:
            self.app.set_display_name(new_name)
        except Exception as e:
            RNS.log(f"Retchat: Error setting display name: {e}", RNS.LOG_ERROR)
        self.db.set_setting("display_name", new_name)
        if announce_now:
            self.announce()

    def announce(self):
        """Broadcast announce packet through NomadNet."""
        try:
            self.app.announce_now()
            RNS.log(f"Retchat: Broadcast announce for {self.delivery_destination_hex}", RNS.LOG_INFO)
        except Exception as e:
            RNS.log(f"Retchat: Failed to announce: {e}", RNS.LOG_ERROR)

    # --- Conversation & Message Management ---
    def conversation(self, source_hash: str) -> Conversation:
        source_hash = source_hash.strip().lower()
        if source_hash not in self._conv_cache:
            messages_path = os.path.join(self.app.conversationpath, source_hash)
            os.makedirs(messages_path, exist_ok=True)
            self._conv_cache[source_hash] = Conversation(source_hash, self.app)
        return self._conv_cache[source_hash]

    def start_conversation(self, source_hash: str) -> Conversation:
        conv = self.conversation(source_hash)
        try:
            conv.scan_storage()
        except Exception:
            pass
        Conversation.query_for_peer(source_hash)
        return conv

    def get_conversations(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        raw_list = Conversation.conversation_list(self.app)
        results = []
        q = query.strip().lower() if query else None

        for entry in raw_list:
            try:
                source_hash, display_name, trust, sort_name, unread, activity, failed = entry[:7]
                source_hash = source_hash.lower()

                custom_name = self.db.get_custom_name(source_hash)
                dn = display_name if (display_name and display_name != "Undefined") else None

                conv = self.conversation(source_hash)
                last_text = ""
                last_time = activity if activity else 0.0

                if conv.messages:
                    sorted_msgs = sorted(conv.messages, key=lambda m: m.sort_timestamp)
                    last_m = sorted_msgs[-1]
                    try:
                        last_text = (last_m.get_content() or "").strip()
                    except Exception:
                        pass
                    try:
                        last_time = last_m.get_timestamp() or last_m.sort_timestamp
                    except Exception:
                        pass
                    try:
                        lm_hash = last_m.get_hash().hex() if last_m.get_hash() else ""
                        info = self._attachment_info(self._attachments(lm_hash, last_m))
                        last_text = self._preview_text(last_text, info)
                    except Exception:
                        pass
                    _release(last_m)

                hops = 0
                try:
                    dest_bytes = bytes.fromhex(source_hash)
                    h = RNS.Transport.hops_to(dest_bytes)
                    if h != RNS.Transport.PATHFINDER_M:
                        hops = h
                except Exception:
                    pass

                if q:
                    match_hash = q in source_hash
                    match_cname = bool(custom_name and q in custom_name.lower())
                    match_dname = bool(dn and q in dn.lower())
                    match_text = bool(last_text and q in last_text.lower())
                    if not (match_hash or match_cname or match_dname or match_text):
                        continue

                results.append({
                    "destination_hash": source_hash,
                    "display_name": dn,
                    "custom_name": custom_name,
                    "last_message_text": last_text,
                    "last_message_time": last_time,
                    "unread_count": unread,
                    "hops": hops,
                })
            except Exception as e:
                RNS.log(f"Retchat: Error loading conversation entry {entry}: {e}", RNS.LOG_ERROR)

        results.sort(key=lambda c: c.get("last_message_time", 0.0), reverse=True)
        return results

    def get_conversation(self, dest_hex: str) -> Optional[Dict[str, Any]]:
        dest_hex = dest_hex.strip().lower()
        convs = self.get_conversations()
        for c in convs:
            if c["destination_hash"] == dest_hex:
                return c
        custom_name = self.db.get_custom_name(dest_hex)
        return {
            "destination_hash": dest_hex,
            "display_name": None,
            "custom_name": custom_name,
            "last_message_text": "",
            "last_message_time": 0.0,
            "unread_count": 0,
            "hops": 0
        }

    def mark_read(self, dest_hex: str):
        try:
            self.app.mark_conversation_read(dest_hex.lower())
        except Exception as e:
            RNS.log(f"Retchat: Error in mark_read: {e}", RNS.LOG_DEBUG)

    def set_custom_name(self, dest_hex: str, new_name: Optional[str]):
        dest_hex = dest_hex.strip().lower()
        self.db.set_custom_name(dest_hex, new_name)

    def delete_conversation(self, dest_hex: str):
        """Remove a contact's conversation from this device.

        Deletes the stored messages (NomadNet), the attachments of those
        messages, unread/failed markers, queued unsent messages, cached
        objects and the local custom name. If the contact writes again, a
        new conversation is created as usual.
        """
        dest_hex = dest_hex.strip().lower()
        if not _HEX_DEST_RE.fullmatch(dest_hex):
            raise ValueError(f"Ungültige Zieladresse: {dest_hex!r}")

        # Attachments are stored per message hash; message files in the
        # conversation directory are named by that hash.
        conv_dir = os.path.join(self.app.conversationpath, dest_hex)
        if os.path.isdir(conv_dir):
            for name in os.listdir(conv_dir):
                att_dir = os.path.join(self.app.attachmentpath, name)
                if _HEX_MSG_RE.fullmatch(name) and os.path.isdir(att_dir):
                    shutil.rmtree(att_dir, ignore_errors=True)

        self.mark_read(dest_hex)  # drops unread/failed markers
        Conversation.delete_conversation(dest_hex, self.app)

        self._conv_cache.pop(dest_hex, None)
        Conversation.cached_conversations.pop(dest_hex, None)
        self._pending.pop(dest_hex, None)
        self.db.delete_conversation(dest_hex)
        RNS.log(f"Retchat: Deleted conversation {dest_hex}", RNS.LOG_INFO)

    IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.svg', '.heic', '.ico', '.tiff')

    @classmethod
    def _is_image_data_or_file(cls, path: Optional[str] = None, data: Optional[bytes] = None, filename: str = '') -> bool:
        if filename and filename.lower().endswith(cls.IMAGE_EXTENSIONS):
            return True
        header = b''
        if data:
            header = data[:16]
        elif path and os.path.isfile(path):
            try:
                with open(path, 'rb') as f:
                    header = f.read(16)
            except Exception:
                return False
        if header.startswith(b'\x89PNG\r\n\x1a\n'):
            return True
        if header.startswith(b'\xff\xd8\xff'):
            return True
        if header.startswith(b'GIF8'):
            return True
        if len(header) >= 12 and header.startswith(b'RIFF') and header[8:12] == b'WEBP':
            return True
        if header.startswith(b'BM'):
            return True
        return False

    # --- Attachments ------------------------------------------------------------
    #
    # NomadNet extracts all attachments of a message (file attachments, image,
    # audio) to <attachmentpath>/<message hash>/ when it stores the message,
    # for sent and received messages alike, and writes a manifest with the
    # original names. Retchat reads that directory; if a message was stored
    # before it was extracted, NomadNet's own extractor is run.

    def _attachments(self, msg_hash: str, source: Any = None) -> List[Dict[str, Any]]:
        """Attachments of a message: [{path, name, size, is_image}].

        ``source`` (ConversationMessage or LXMessage) is only used if the
        attachments haven't been extracted yet.
        """
        if not msg_hash:
            return []
        att_dir = os.path.join(self.app.attachmentpath, msg_hash)
        if not os.path.isdir(att_dir) and source is not None:
            lxm = source if isinstance(source, LXMF.LXMessage) else None
            if lxm is None:
                try:
                    # Answered from NomadNet's index, so text messages aren't read from disk.
                    if not source.has_attachments():
                        return []
                    if not source.loaded:
                        source.load()
                    lxm = source.lxm
                except Exception:
                    return []
            if lxm is not None and not os.path.isdir(att_dir):
                try:
                    ConversationMessage.extract_attachments_from_lxm(lxm, self.app)
                except Exception as e:
                    RNS.log(f"Retchat: Error extracting attachments: {e}", RNS.LOG_ERROR)
        return self._read_attachment_dir(att_dir)

    def _read_attachment_dir(self, att_dir: str) -> List[Dict[str, Any]]:
        if not os.path.isdir(att_dir):
            return []
        entries = []
        try:
            with open(os.path.join(att_dir, "manifest"), "rb") as f:
                manifest = msgpack.unpackb(f.read(), raw=False)
            for entry in manifest.get("files", []):
                stored = os.path.basename(str(entry.get("stored_name", "")))
                entries.append((stored, entry.get("name") or stored))
        except (OSError, ValueError, msgpack.UnpackException):
            entries = [(n, n) for n in sorted(os.listdir(att_dir)) if n != "manifest"]

        out = []
        for stored, name in entries:
            path = os.path.join(att_dir, stored)
            if stored and os.path.isfile(path):
                out.append({
                    "path": path,
                    "name": name,
                    "size": os.path.getsize(path),
                    "is_image": self._is_image_data_or_file(path=path, filename=name),
                })
        return out

    @staticmethod
    def _attachment_info(attachments: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Message dict keys: the first image is shown inline, everything else as files."""
        image = next((a for a in attachments if a["is_image"]), None)
        return {
            "image_path": image["path"] if image else None,
            "image_name": image["name"] if image else None,
            "image_size": image["size"] if image else None,
            "files": [{"path": a["path"], "name": a["name"], "size": a["size"]}
                      for a in attachments if a is not image],
        }

    @staticmethod
    def _preview_text(text: str, info: Dict[str, Any]) -> str:
        """Chat list preview of a message with attachments."""
        if info["image_path"]:
            return f"📷 {text}" if text else "📷 Bild"
        if info["files"]:
            return f"📎 {text}" if text else f"📎 {info['files'][0]['name']}"
        return text

    def _prepare_attachment(self, path: str) -> Tuple[Dict[int, Any], Dict[str, Any]]:
        """LXMF fields for sending ``path``, and display info for the local bubble.

        Images are downscaled and sent as image field (shown inline); any other
        file is sent unchanged as file attachment. Raises ValueError if the file
        can't be read or exceeds MAX_ATTACHMENT_SIZE.
        """
        name = os.path.basename(path)
        if is_sendable_image(path):
            data, fmt, _info = self._prepare_outgoing_image(path)
            if data:
                _check_attachment_size(len(data))
                return ({LXMF.FIELD_IMAGE: [fmt, data]},
                        {"image_path": path, "image_name": name, "image_size": len(data), "files": []})
        try:
            size = os.path.getsize(path)
            _check_attachment_size(size)
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            raise ValueError(f"Datei kann nicht gelesen werden: {e.strerror or e}") from e
        return ({LXMF.FIELD_FILE_ATTACHMENTS: [[name, data]]},
                {"image_path": None, "image_name": None, "image_size": None,
                 "files": [{"path": path, "name": name, "size": len(data)}]})

    def _prepare_outgoing_image(self, image_path: str) -> Tuple[Optional[bytes], str, Optional[Dict[str, Any]]]:
        try:
            filename = os.path.basename(image_path)
            # Try resizing/compressing with PIL if available
            try:
                from PIL import Image
                import io
                with Image.open(image_path) as im:
                    max_dim = 800
                    w, h = im.size
                    if w > max_dim or h > max_dim:
                        scale = min(max_dim / w, max_dim / h)
                        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                        resample_filter = getattr(Image.Resampling, "LANCZOS", Image.LANCZOS)
                        im = im.resize(new_size, resample_filter)

                    buf = io.BytesIO()
                    try:
                        im.save(buf, format="WEBP", quality=75)
                        data = buf.getvalue()
                        fmt = "webp"
                    except Exception:
                        buf = io.BytesIO()
                        im_rgb = im.convert("RGB")
                        im_rgb.save(buf, format="JPEG", quality=75)
                        data = buf.getvalue()
                        fmt = "jpg"
            except Exception:
                with open(image_path, "rb") as f:
                    data = f.read()
                ext = os.path.splitext(image_path)[1].lower().lstrip(".")
                fmt = ext if ext in ("png", "jpg", "jpeg", "webp") else "jpg"

            return data, fmt, {"path": image_path, "name": filename, "size": len(data)}
        except Exception as e:
            RNS.log(f"Retchat: Error preparing outgoing image: {e}", RNS.LOG_ERROR)
            return None, "", None

    def get_messages(self, dest_hex: str) -> List[Dict[str, Any]]:
        dest_hex = dest_hex.strip().lower()
        conv = self.conversation(dest_hex)
        try:
            conv.scan_storage()
        except Exception:
            pass

        sorted_msgs = sorted(conv.messages, key=lambda m: m.sort_timestamp)
        own_hash = self.delivery_destination_hex.lower()
        out = []

        # The getters use NomadNet's per-message cache/index. It drops the cached
        # state when a message file changes (scan_storage), so states stay current
        # without reading every message file again.
        for m in sorted_msgs:
            try:
                content = (m.get_content() or "").strip()
                title = m.get_title() or ""
                ts = m.get_timestamp() or m.sort_timestamp
                raw_state = m.get_state()
                msg_hash = m.get_hash().hex() if m.get_hash() else f"msg_{int(ts*1000)}"

                src = _source_hash(m)
                is_outgoing = bool(src) and src.hex().lower() == own_hash

                hops = 0  # LXMF messages don't carry a hop count
                state = map_lxmf_state_to_ui(raw_state, is_outgoing)
                info = self._attachment_info(self._attachments(msg_hash, m))

                out.append({
                    "message_hash": msg_hash,
                    "conversation_hash": dest_hex,
                    "sender_hash": own_hash if is_outgoing else dest_hex,
                    "recipient_hash": dest_hex if is_outgoing else own_hash,
                    "is_outgoing": is_outgoing,
                    "content": content,
                    "title": title,
                    "timestamp": ts,
                    "state": state,
                    "hops": hops,
                    **info,
                })
            except Exception as e:
                RNS.log(f"Retchat: Error loading message: {e}", RNS.LOG_ERROR)
            finally:
                _release(m)

        return out

    def send_message(self, dest_hex: str, content: str, attachment_path: Optional[str] = None) -> Dict[str, Any]:
        """Send a message, optionally with an image or any other file (see _prepare_attachment)."""
        dest_hex = dest_hex.strip().lower()
        if len(dest_hex) != 32:
            raise ValueError("Ungültige Zieladresse: Muss ein 32-Zeichen Hex-Hash sein.")

        conv = self.conversation(dest_hex)
        now = time.time()

        fields = None
        info = self._attachment_info([])
        if attachment_path:
            fields, info = self._prepare_attachment(attachment_path)

        msg_hash = None
        if self.peer_known(dest_hex):
            try:
                sent = conv.send(content=content, fields=fields)
            except Exception as e:
                RNS.log(f"Retchat: Error sending message to {dest_hex}: {e}", RNS.LOG_ERROR)
                raise
            if sent and conv.messages:
                # NomadNet appends the sent message itself; its file is named by
                # the LXMF hash, so this needs no disk read. Delivery state
                # changes arrive via _on_outbound_state() with that hash.
                sent_m = conv.messages[-1]
                if sent_m.get_hash():
                    msg_hash = sent_m.get_hash().hex()
                _release(sent_m)
                # Show what was actually sent (e.g. the downscaled image), as
                # extracted by NomadNet when it stored the message.
                sent_atts = self._attachments(msg_hash) if msg_hash else []
                if sent_atts:
                    info = self._attachment_info(sent_atts)

        if msg_hash is None:
            # Recipient not known yet: queue until its announce/path arrives
            # (flush_pending), then report the final hash via the message id
            # callbacks so the shown bubble can follow the delivery state.
            msg_hash = f"out_{os.urandom(8).hex()}"
            self._pending.setdefault(dest_hex, []).append((content, attachment_path, msg_hash))
            Conversation.query_for_peer(dest_hex)

        hops = 0
        try:
            h = RNS.Transport.hops_to(bytes.fromhex(dest_hex))
            if h != RNS.Transport.PATHFINDER_M:
                hops = h
        except Exception:
            pass

        return {
            "message_hash": msg_hash,
            "conversation_hash": dest_hex,
            "sender_hash": self.delivery_destination_hex,
            "recipient_hash": dest_hex,
            "is_outgoing": True,
            "content": content,
            "timestamp": now,
            "state": STATE_SENDING,
            "hops": hops,
            **info,
        }

    def peer_known(self, dest_hex: str) -> bool:
        try:
            return self.conversation(dest_hex).ensure_send_destination()
        except Exception:
            return False

    def flush_pending(self) -> List[str]:
        if not self._pending:
            return []
        sent = []
        for dest_hex in list(self._pending):
            if not self.peer_known(dest_hex):
                Conversation.query_for_peer(dest_hex)
                continue
            queued = self._pending.pop(dest_hex)
            for content, img_p, temp_hash in queued:
                try:
                    result = self.send_message(dest_hex, content, attachment_path=img_p)
                    if result["message_hash"] != temp_hash:
                        GLib.idle_add(self._notify_message_id, temp_hash, result["message_hash"])
                except Exception as e:
                    RNS.log(f"Retchat: Failed to send pending message: {e}", RNS.LOG_ERROR)
            sent.append(dest_hex)
            GLib.idle_add(self._notify_conversations_changed)
        return sent

    def _background_worker(self):
        while not self._poll_stop:
            time.sleep(3.0)
            if self._poll_stop:
                break
            try:
                self.flush_pending()
            except Exception as e:
                RNS.log(f"Retchat: Error in background worker: {e}", RNS.LOG_DEBUG)

    # --- Announces & Path Finding ---
    def get_announces(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        stream = getattr(self.app.directory, "announce_stream", [])
        seen = set()
        results = []
        q = query.strip().lower() if query else None

        for item in stream:
            try:
                timestamp, source_hash, app_data, kind = item[:4]
                dest_hex = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash).lower()

                if dest_hex in seen:
                    continue
                seen.add(dest_hex)

                display_name = None
                if app_data:
                    if isinstance(app_data, bytes):
                        display_name = app_data.decode("utf-8", errors="replace")
                    else:
                        display_name = str(app_data)

                hops = 0
                try:
                    dest_bytes = source_hash if isinstance(source_hash, bytes) else bytes.fromhex(dest_hex)
                    h = RNS.Transport.hops_to(dest_bytes)
                    if h != RNS.Transport.PATHFINDER_M:
                        hops = h
                except Exception:
                    pass

                if q:
                    match_hex = q in dest_hex
                    match_name = bool(display_name and q in display_name.lower())
                    if not (match_hex or match_name):
                        continue

                results.append({
                    "destination_hash": dest_hex,
                    "display_name": display_name,
                    "hops": hops,
                    "aspect": f"nomadnet.{kind}",
                    "receiving_interface": "",
                    "last_seen": timestamp
                })
            except Exception as e:
                RNS.log(f"Retchat: Error parsing announce stream item: {e}", RNS.LOG_DEBUG)

        return results

    def request_path(
        self,
        dest_hex: str,
        callback: Optional[Callable[[str, Optional[int], bool], None]] = None
    ):
        dest_hex = dest_hex.strip().lower()
        try:
            dest_bytes = bytes.fromhex(dest_hex)
        except Exception as e:
            RNS.log(f"Retchat: Invalid destination hash {dest_hex}: {e}", RNS.LOG_ERROR)
            if callback:
                GLib.idle_add(callback, dest_hex, None, False)
            return

        def _worker():
            try:
                t_start = time.time()
                RNS.Transport.request_path(dest_bytes)
                Conversation.query_for_peer(dest_hex)
                deadline = time.time() + 6.0
                while time.time() < deadline:
                    if dest_bytes in RNS.Transport.path_table:
                        entry = RNS.Transport.path_table[dest_bytes]
                        if entry and entry[0] >= t_start:
                            hops = RNS.Transport.hops_to(dest_bytes)
                            if hops == RNS.Transport.PATHFINDER_M:
                                hops = 0
                            GLib.idle_add(self._notify_path_resolved, dest_hex, hops)
                            if callback:
                                GLib.idle_add(callback, dest_hex, hops, True)
                            return
                    time.sleep(0.1)

                if dest_bytes in RNS.Transport.path_table:
                    hops = RNS.Transport.hops_to(dest_bytes)
                    if hops == RNS.Transport.PATHFINDER_M:
                        hops = 0
                    GLib.idle_add(self._notify_path_resolved, dest_hex, hops)
                    if callback:
                        GLib.idle_add(callback, dest_hex, hops, False)
                else:
                    if callback:
                        GLib.idle_add(callback, dest_hex, None, False)
            except Exception as e:
                RNS.log(f"Retchat: Error in path worker for {dest_hex}: {e}", RNS.LOG_ERROR)
                if callback:
                    GLib.idle_add(callback, dest_hex, None, False)

        threading.Thread(target=_worker, daemon=True, name=f"Path-{dest_hex[:8]}").start()

    # --- Inbound Message Handling ---
    def _on_inbound_lxmessage(self, message: LXMF.LXMessage):
        try:
            source_hash = RNS.hexrep(message.source_hash, delimit=False).lower()
            content = message.content_as_string()
            msg_hash = message.hash.hex()
            hops = RNS.Transport.hops_to(message.source_hash)
            if hops == RNS.Transport.PATHFINDER_M:
                hops = 0

            info = self._attachment_info(self._attachments(msg_hash, message))

            self._trigger_notification(source_hash, self._preview_text(content, info))

            msg_dict = {
                "message_hash": msg_hash,
                "conversation_hash": source_hash,
                "sender_hash": source_hash,
                "recipient_hash": self.delivery_destination_hex,
                "is_outgoing": False,
                "content": content,
                "timestamp": message.timestamp or time.time(),
                "state": STATE_DELIVERED,
                "hops": hops,
                **info,
            }

            GLib.idle_add(self._notify_message_received, msg_dict)
            GLib.idle_add(self._notify_conversations_changed)
        except Exception as e:
            RNS.log(f"Retchat: Error handling inbound message: {e}", RNS.LOG_ERROR)

    def _trigger_notification(self, sender_hex: str, preview: str):
        try:
            custom_name = self.db.get_custom_name(sender_hex)
            sender_name = custom_name or sender_hex[:8]
            preview = (preview[:60] + "...") if len(preview) > 60 else preview
            subprocess.Popen([
                "notify-send",
                "-a", "Retchat",
                "-i", "mail-message-new-symbolic",
                f"Nachricht von {sender_name}",
                preview
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # --- Callbacks ---
    def add_message_received_callback(self, cb: Callable[[Dict[str, Any]], None]):
        self._message_received_callbacks.append(cb)

    def add_message_state_callback(self, cb: Callable[[str, int], None]):
        self._message_state_callbacks.append(cb)

    def add_message_id_callback(self, cb: Callable[[str, str], None]):
        """cb(temporary_hash, lxmf_hash): a queued message was sent and got its final id."""
        self._message_id_callbacks.append(cb)

    def add_announce_callback(self, cb: Callable[[Dict[str, Any]], None]):
        self._announce_callbacks.append(cb)

    def add_path_resolved_callback(self, cb: Callable[[str, int], None]):
        self._path_resolved_callbacks.append(cb)

    def add_conversations_changed_callback(self, cb: Callable[[], None]):
        self._conversations_changed_callbacks.append(cb)

    def _on_conversations_changed_nomadnet(self, *args):
        GLib.idle_add(self._notify_conversations_changed)

    def _notify_message_received(self, msg_dict: Dict[str, Any]) -> bool:
        for cb in self._message_received_callbacks:
            try:
                cb(msg_dict)
            except Exception as e:
                RNS.log(f"Retchat: Error in message received callback: {e}", RNS.LOG_ERROR)
        return False

    def _on_outbound_state(self, message: LXMF.LXMessage):
        """Delivery state of a sent message changed (Reticulum thread)."""
        if message.hash is None:
            return
        state = map_lxmf_state_to_ui(message.state, is_outgoing=True)
        GLib.idle_add(self._notify_message_state, message.hash.hex(), state)

    def _notify_message_id(self, old_hash: str, new_hash: str) -> bool:
        for cb in self._message_id_callbacks:
            try:
                cb(old_hash, new_hash)
            except Exception as e:
                RNS.log(f"Retchat: Error in message id callback: {e}", RNS.LOG_ERROR)
        return False

    def _notify_message_state(self, message_hash: str, state: int) -> bool:
        for cb in self._message_state_callbacks:
            try:
                cb(message_hash, state)
            except Exception as e:
                RNS.log(f"Retchat: Error in message state callback: {e}", RNS.LOG_ERROR)
        return False

    def _notify_announce(self, data: Dict[str, Any]) -> bool:
        for cb in self._announce_callbacks:
            try:
                cb(data)
            except Exception as e:
                RNS.log(f"Retchat: Error in announce callback: {e}", RNS.LOG_ERROR)
        return False

    def _notify_path_resolved(self, dest_hex: str, hops: int) -> bool:
        for cb in self._path_resolved_callbacks:
            try:
                cb(dest_hex, hops)
            except Exception as e:
                RNS.log(f"Retchat: Error in path resolved callback: {e}", RNS.LOG_ERROR)
        return False

    def _notify_conversations_changed(self) -> bool:
        for cb in self._conversations_changed_callbacks:
            try:
                cb()
            except Exception as e:
                RNS.log(f"Retchat: Error in conversations changed callback: {e}", RNS.LOG_ERROR)
        return False

    # --- Interfaces ---
    def get_configured_interfaces(self) -> List[Dict[str, Any]]:
        """Read all configured interfaces from ~/.reticulum/config, cross-referenced with live status."""
        cfg_dir = os.path.expanduser("~/.reticulum")
        if os.path.isdir(os.path.expanduser("~/.config/reticulum")) and os.path.isfile(os.path.expanduser("~/.config/reticulum/config")):
            cfg_dir = os.path.expanduser("~/.config/reticulum")
        cfg_path = os.path.join(cfg_dir, "config")

        if not os.path.exists(cfg_path):
            return []

        from RNS.vendor.configobj import ConfigObj
        try:
            cfg = ConfigObj(cfg_path)
            raw_interfaces = cfg.get("interfaces", {})
        except Exception as e:
            RNS.log(f"Retchat: Error reading interfaces from config: {e}", RNS.LOG_ERROR)
            return []

        live_by_name = {}
        for iface in RNS.Transport.interfaces:
            name = getattr(iface, "name", str(iface))
            live_by_name[name] = iface

        results = []
        for name, c in raw_interfaces.items():
            if not isinstance(c, dict):
                continue
            itype = c.get("type", "UnknownInterface")

            en = str(c.get("enabled", "")).lower()
            ifen = str(c.get("interface_enabled", "")).lower()
            b_en = en in ("true", "yes", "1")
            b_ifen = ifen in ("true", "yes", "1")
            is_enabled = b_en or b_ifen

            live_iface = live_by_name.get(name)
            is_online = getattr(live_iface, "online", False) if live_iface else False
            rxb = getattr(live_iface, "rxb", 0) if live_iface else 0
            txb = getattr(live_iface, "txb", 0) if live_iface else 0

            details = []
            if itype in ("TCPClientInterface", "TCPInterface"):
                host = c.get("target_host") or ""
                port = c.get("target_port") or ""
                if host:
                    details.append(f"{host}:{port}")
            elif itype == "RNodeInterface":
                port = c.get("port") or ""
                freq = c.get("frequency")
                if port:
                    details.append(f"Port: {port}")
                if freq:
                    try:
                        freq_mhz = int(freq) / 1000000.0
                        details.append(f"{freq_mhz:.3f} MHz")
                    except Exception:
                        pass
            elif itype == "AutoInterface":
                details.append("Lokales Multicast")

            results.append({
                "name": name,
                "type": itype,
                "enabled": is_enabled,
                "online": is_online,
                "rxb": rxb,
                "txb": txb,
                "details": " • ".join(details) if details else itype,
                "config": dict(c)
            })

        return results

    def set_interface_enabled(self, name: str, enabled: bool) -> bool:
        """Toggle an interface enabled/disabled in ~/.reticulum/config and update live state."""
        cfg_dir = os.path.expanduser("~/.reticulum")
        if os.path.isdir(os.path.expanduser("~/.config/reticulum")) and os.path.isfile(os.path.expanduser("~/.config/reticulum/config")):
            cfg_dir = os.path.expanduser("~/.config/reticulum")
        cfg_path = os.path.join(cfg_dir, "config")

        if not os.path.exists(cfg_path):
            return False

        from RNS.vendor.configobj import ConfigObj
        try:
            cfg = ConfigObj(cfg_path)
            if "interfaces" not in cfg or name not in cfg["interfaces"]:
                return False

            iface_cfg = cfg["interfaces"][name]
            if enabled:
                iface_cfg["enabled"] = "yes"
                iface_cfg["interface_enabled"] = "true"
            else:
                iface_cfg["enabled"] = "no"
                iface_cfg["interface_enabled"] = "false"

            cfg.write()

            # Dynamic live update: if disabling, detach it so it stops reconnecting / holding ports
            if not enabled:
                for iface in RNS.Transport.interfaces:
                    if getattr(iface, "name", None) == name:
                        try:
                            if hasattr(iface, "detach"):
                                iface.detach()
                            iface.online = False
                            RNS.log(f"Retchat: Detached interface '{name}'", RNS.LOG_NOTICE)
                        except Exception as e:
                            RNS.log(f"Retchat: Error detaching interface '{name}': {e}", RNS.LOG_WARNING)
            return True
        except Exception as e:
            RNS.log(f"Retchat: Failed to toggle interface '{name}': {e}", RNS.LOG_ERROR)
            return False

    def get_interfaces_info(self) -> List[Dict[str, Any]]:
        result = []
        for iface in RNS.Transport.interfaces:
            info = {
                "name": getattr(iface, "name", str(iface)),
                "type": type(iface).__name__,
                "online": getattr(iface, "online", True),
                "rxb": getattr(iface, "rxb", 0),
                "txb": getattr(iface, "txb", 0),
                "mode": getattr(iface, "mode", "normal")
            }
            result.append(info)
        return result

    # ------------------------------------------------------------------ #
    # LXMF Propagation Node Sync
    # ------------------------------------------------------------------ #
    def get_propagation_node_info(self) -> Dict[str, Any]:
        """Get propagation node configuration and current active node."""
        if not self.app:
            return {"configured": "", "active": "", "is_auto": True}

        user_node = self.app.get_user_selected_propagation_node()
        default_node = self.app.get_default_propagation_node()

        configured_hex = user_node.hex() if isinstance(user_node, (bytes, bytearray)) else ""
        active_hex = default_node.hex() if isinstance(default_node, (bytes, bytearray)) else ""

        return {
            "configured": configured_hex,
            "active": active_hex,
            "is_auto": user_node is None
        }

    def set_propagation_node(self, node_hex: Optional[str]) -> Tuple[bool, str]:
        """Set user-selected propagation node by hex hash, or None for auto-selection."""
        if not self.app:
            return False, "NomadNet-Backend nicht bereit"

        if not node_hex or not node_hex.strip():
            try:
                self.app.set_user_selected_propagation_node(None)
                active = self.app.get_default_propagation_node()
                active_hex = active.hex() if isinstance(active, (bytes, bytearray)) else "Keiner"
                return True, f"Automatischer Modus aktiv (Node: {active_hex[:8]}...)"
            except Exception as e:
                return False, f"Fehler beim Zurücksetzen: {e}"

        clean_hex = node_hex.strip().lower()
        if len(clean_hex) != 32:
            return False, "Der Node-Hash muss genau 32 Hex-Zeichen (16 Bytes) lang sein"

        try:
            node_bytes = bytes.fromhex(clean_hex)
        except ValueError:
            return False, "Ungültiger Hex-Wert"

        try:
            self.app.set_user_selected_propagation_node(node_bytes)
            if not RNS.Transport.has_path(node_bytes):
                RNS.Transport.request_path(node_bytes)
            return True, f"Propagation-Node [{clean_hex[:8]}...{clean_hex[-4:]}] gespeichert!"
        except Exception as e:
            return False, f"Fehler beim Speichern: {e}"

    def request_sync(self) -> Tuple[bool, str]:
        """Fetch all messages waiting for us on the (default or auto-selected) propagation node."""
        if not self.app:
            return False, "NomadNet-Backend nicht bereit"

        try:
            if not self.app.get_default_propagation_node():
                self.app.autoselect_propagation_node()

            pnode = self.app.get_default_propagation_node()
            if not pnode:
                return False, "Kein Propagation-Node im Mesh verfügbar"

            self.app.request_lxmf_sync()
            pnode_hex = pnode.hex() if isinstance(pnode, bytes) else str(pnode)
            return True, f"Synchronisierung mit Node [{pnode_hex[:8]}...{pnode_hex[-4:]}] gestartet..."
        except Exception as e:
            return False, f"Sync fehlgeschlagen: {e}"

    def get_sync_status(self) -> str:
        if not self.app:
            return "Idle"
        try:
            return self.app.get_sync_status()
        except Exception:
            return "Idle"

    def get_sync_status_text(self) -> str:
        if not self.app:
            return "Nicht bereit"
        status = self.get_sync_status()
        translations = {
            "Idle": "Bereit",
            "Path requested": "Pfad zum Propagation-Node wird gesucht...",
            "Establishing link": "Verbindung zum Propagation-Node wird aufgebaut...",
            "Link established": "Verbindung zum Propagation-Node hergestellt",
            "Sync request sent": "Sync-Anfrage an Node gesendet...",
            "Receiving messages": "Nachrichten werden empfangen...",
            "Messages received": "Nachrichten empfangen",
            "No path to node": "Kein Pfad zum Propagation-Node",
            "Link establisment failed": "Verbindungsaufbau zum Node fehlgeschlagen",
            "Sync request failed": "Sync-Anfrage fehlgeschlagen",
            "Remote got no identity": "Node hat keine Identität übermittelt",
            "Node rejected request": "Node hat Sync-Anfrage abgelehnt",
            "Sync failed": "Synchronisierung fehlgeschlagen",
            "Done, no new messages": "Sync abgeschlossen (keine neuen Nachrichten)",
        }
        if status in translations:
            return translations[status]
        if status and status.startswith("Downloaded "):
            parts = status.split()
            count = parts[1] if len(parts) > 1 else ""
            return f"Sync abgeschlossen: {count} neue Nachricht(en) empfangen!"
        return status or "Unbekannt"

    def get_sync_progress(self) -> float:
        if not self.app:
            return 0.0
        try:
            return self.app.get_sync_progress()
        except Exception:
            return 0.0

    def shutdown(self):
        """Clean shutdown of background threads and NomadNet."""
        self._poll_stop = True
        try:
            self.app.exit_handler()
        except Exception as e:
            RNS.log(f"Retchat: Error exiting NomadNet: {e}", RNS.LOG_ERROR)
