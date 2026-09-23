"""Reticulum and NomadNet LXMF Service integration for Retchat."""

import getpass
import os
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
        self._announce_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._path_resolved_callbacks: List[Callable[[str, int], None]] = []
        self._conversations_changed_callbacks: List[Callable[[], None]] = []
        self._rrc_message_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._rrc_change_callbacks: List[Callable[[Optional[str]], None]] = []

        # Ensure ~/.reticulum/config has TCP enabled as default, and enable_transport=False
        self._ensure_tcp_default_config()

        # Install quiet UI monkeypatch before creating NomadNetworkApp
        install_quiet_ui()

        Conversation.created_callback = self._on_conversations_changed_nomadnet
        self.app = _RetchatApp(self, configdir=self._configdir, rnsconfigdir=self._rnsconfigdir)

        # Wire RRC (Reticulum Relay Chat) callbacks
        if hasattr(self.app, "rrc") and self.app.rrc:
            self.app.rrc.set_message_callback(self._on_nomadnet_rrc_message)
            self.app.rrc.set_change_callback(self._on_nomadnet_rrc_change)

        self._conv_cache: Dict[str, Conversation] = {}
        self._pending: Dict[str, List[str]] = {}

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
                        if self._find_image_for_message(last_m, lm_hash):
                            if last_text:
                                last_text = f"📷 {last_text}"
                            else:
                                last_text = "📷 Bild"
                    except Exception:
                        pass

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

    def _find_image_for_message(self, m: Any, msg_hash: str) -> Optional[Dict[str, Any]]:
        if not msg_hash:
            return None

        # 1. Check attachment directory on disk (NomadNet extracted path)
        att_dir = os.path.join(self.app.attachmentpath, msg_hash)
        if os.path.isdir(att_dir):
            manifest_p = os.path.join(att_dir, "manifest")
            if os.path.isfile(manifest_p):
                try:
                    with open(manifest_p, "rb") as f:
                        manifest = msgpack.unpackb(f.read(), raw=False)
                    for entry in manifest.get("files", []):
                        stored_name = entry.get("stored_name", "")
                        fname = entry.get("name", "")
                        fpath = os.path.join(att_dir, stored_name)
                        if os.path.isfile(fpath) and self._is_image_data_or_file(path=fpath, filename=fname):
                            size = entry.get("size")
                            if size is None:
                                size = os.path.getsize(fpath)
                            return {
                                "path": fpath,
                                "name": fname or "image.jpg",
                                "size": size
                            }
                except Exception as e:
                    RNS.log(f"Retchat: Error reading attachment manifest: {e}", RNS.LOG_DEBUG)

            # Fallback for files in att_dir without manifest
            try:
                for fname in os.listdir(att_dir):
                    if fname == "manifest":
                        continue
                    fpath = os.path.join(att_dir, fname)
                    if os.path.isfile(fpath) and self._is_image_data_or_file(path=fpath, filename=fname):
                        return {
                            "path": fpath,
                            "name": fname if fname.lower().endswith(self.IMAGE_EXTENSIONS) else f"{fname}.jpg",
                            "size": os.path.getsize(fpath)
                        }
            except Exception:
                pass

        # 2. Check if m has in-memory fields or unstripped fields
        try:
            fields = m.get_fields() if hasattr(m, "get_fields") else {}
            if fields and isinstance(fields, dict):
                if LXMF.FIELD_IMAGE in fields:
                    fmt, data = ConversationMessage._unpack_media_field(fields[LXMF.FIELD_IMAGE])
                    if data and isinstance(data, bytes) and len(data) > 0:
                        os.makedirs(att_dir, exist_ok=True)
                        ext = ConversationMessage._ext_from_media_format(fmt, data)
                        fname = f"image{ext}"
                        stored_name = "file_0"
                        fpath = os.path.join(att_dir, stored_name)
                        with open(fpath, "wb") as f:
                            f.write(data)
                        try:
                            manifest = {"files": [{"name": fname, "stored_name": stored_name, "size": len(data)}]}
                            with open(os.path.join(att_dir, "manifest"), "wb") as mf:
                                mf.write(msgpack.packb(manifest))
                        except Exception:
                            pass
                        return {"path": fpath, "name": fname, "size": len(data)}

                if LXMF.FIELD_FILE_ATTACHMENTS in fields:
                    for idx, att in enumerate(fields[LXMF.FIELD_FILE_ATTACHMENTS]):
                        if isinstance(att, list) and len(att) >= 2:
                            att_name = ConversationMessage.safe_attachment_name(att[0], fallback=f"attachment_{idx}")
                            att_data = att[1] if isinstance(att[1], bytes) else b""
                            if self._is_image_data_or_file(data=att_data, filename=att_name):
                                os.makedirs(att_dir, exist_ok=True)
                                stored_name = f"file_{idx}"
                                fpath = os.path.join(att_dir, stored_name)
                                with open(fpath, "wb") as f:
                                    f.write(att_data)
                                return {"path": fpath, "name": att_name, "size": len(att_data)}
        except Exception as e:
            RNS.log(f"Retchat: Error extracting image from fields: {e}", RNS.LOG_DEBUG)

        return None

    def _extract_image_from_lxmessage(self, message: LXMF.LXMessage) -> Optional[Dict[str, Any]]:
        msg_hash = message.hash.hex()
        att_dir = os.path.join(self.app.attachmentpath, msg_hash)

        # Check if NomadNet ingest already extracted it
        if os.path.isdir(att_dir):
            manifest_p = os.path.join(att_dir, "manifest")
            if os.path.isfile(manifest_p):
                try:
                    with open(manifest_p, "rb") as f:
                        manifest = msgpack.unpackb(f.read(), raw=False)
                    for entry in manifest.get("files", []):
                        stored_name = entry.get("stored_name", "")
                        fname = entry.get("name", "")
                        fpath = os.path.join(att_dir, stored_name)
                        if os.path.isfile(fpath) and self._is_image_data_or_file(path=fpath, filename=fname):
                            return {
                                "path": fpath,
                                "name": fname or "image.jpg",
                                "size": entry.get("size", os.path.getsize(fpath))
                            }
                except Exception:
                    pass

        # Check fields directly on message
        try:
            fields = message.get_fields() if hasattr(message, "get_fields") else {}
            if fields and isinstance(fields, dict):
                if LXMF.FIELD_IMAGE in fields:
                    fmt, data = ConversationMessage._unpack_media_field(fields[LXMF.FIELD_IMAGE])
                    if data and isinstance(data, bytes) and len(data) > 0:
                        os.makedirs(att_dir, exist_ok=True)
                        ext = ConversationMessage._ext_from_media_format(fmt, data)
                        fname = f"image{ext}"
                        stored_name = "file_0"
                        fpath = os.path.join(att_dir, stored_name)
                        with open(fpath, "wb") as f:
                            f.write(data)
                        try:
                            manifest = {"files": [{"name": fname, "stored_name": stored_name, "size": len(data)}]}
                            with open(os.path.join(att_dir, "manifest"), "wb") as mf:
                                mf.write(msgpack.packb(manifest))
                        except Exception:
                            pass
                        return {"path": fpath, "name": fname, "size": len(data)}

                if LXMF.FIELD_FILE_ATTACHMENTS in fields:
                    for idx, att in enumerate(fields[LXMF.FIELD_FILE_ATTACHMENTS]):
                        if isinstance(att, list) and len(att) >= 2:
                            att_name = ConversationMessage.safe_attachment_name(att[0], fallback=f"attachment_{idx}")
                            att_data = att[1] if isinstance(att[1], bytes) else b""
                            if self._is_image_data_or_file(data=att_data, filename=att_name):
                                os.makedirs(att_dir, exist_ok=True)
                                stored_name = f"file_{idx}"
                                fpath = os.path.join(att_dir, stored_name)
                                with open(fpath, "wb") as f:
                                    f.write(att_data)
                                return {"path": fpath, "name": att_name, "size": len(att_data)}
        except Exception as e:
            RNS.log(f"Retchat: Error extracting live inbound image: {e}", RNS.LOG_ERROR)

        return None

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

        for m in sorted_msgs:
            try:
                m.load()
                content = (m.get_content() or "").strip()
                title = m.get_title() or ""
                ts = m.get_timestamp() or m.sort_timestamp
                raw_state = m.get_state()
                msg_hash = m.get_hash().hex() if m.get_hash() else f"msg_{int(ts*1000)}"

                is_outgoing = False
                if m.lxm is not None and m.lxm.source_hash:
                    src_hex = m.lxm.source_hash.hex().lower()
                    is_outgoing = (src_hex == own_hash)

                hops = getattr(m.lxm, "hops", 0) if m.lxm else 0
                state = map_lxmf_state_to_ui(raw_state, is_outgoing)
                image_info = self._find_image_for_message(m, msg_hash)

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
                    "image_path": image_info["path"] if image_info else None,
                    "image_name": image_info["name"] if image_info else None,
                    "image_size": image_info["size"] if image_info else None,
                })
            except Exception as e:
                RNS.log(f"Retchat: Error loading message: {e}", RNS.LOG_ERROR)

        return out

    def send_message(self, dest_hex: str, content: str, image_path: Optional[str] = None) -> Dict[str, Any]:
        dest_hex = dest_hex.strip().lower()
        if len(dest_hex) != 32:
            raise ValueError("Ungültige Zieladresse: Muss ein 32-Zeichen Hex-Hash sein.")

        conv = self.conversation(dest_hex)
        now = time.time()
        temp_hash = f"out_{int(now*1000)}"

        # Prepare image field if image_path is provided
        fields = None
        img_info = None
        if image_path and os.path.isfile(image_path):
            img_bytes, fmt, img_info = self._prepare_outgoing_image(image_path)
            if img_bytes:
                fields = {LXMF.FIELD_IMAGE: [fmt, img_bytes]}

        if not self.peer_known(dest_hex):
            self._pending.setdefault(dest_hex, []).append((content, image_path))
            Conversation.query_for_peer(dest_hex)
            return {
                "message_hash": temp_hash,
                "conversation_hash": dest_hex,
                "sender_hash": self.delivery_destination_hex,
                "recipient_hash": dest_hex,
                "is_outgoing": True,
                "content": content,
                "timestamp": now,
                "state": STATE_SENDING,
                "hops": 0,
                "image_path": img_info["path"] if img_info else image_path,
                "image_name": img_info["name"] if img_info else (os.path.basename(image_path) if image_path else None),
                "image_size": img_info["size"] if img_info else None,
            }

        try:
            conv.send(content=content, fields=fields)
        except Exception as e:
            RNS.log(f"Retchat: Error sending message to {dest_hex}: {e}", RNS.LOG_ERROR)
            raise

        try:
            conv.scan_storage()
        except Exception:
            pass

        msg_hash = temp_hash
        if conv.messages:
            last_m = sorted(conv.messages, key=lambda m: m.sort_timestamp)[-1]
            try:
                last_m.load()
                if last_m.get_hash():
                    msg_hash = last_m.get_hash().hex()
                if last_m.lxm:
                    def _cb_delivered(m):
                        h = m.hash.hex()
                        GLib.idle_add(self._notify_message_state, h, STATE_DELIVERED)
                    def _cb_failed(m):
                        h = m.hash.hex()
                        GLib.idle_add(self._notify_message_state, h, STATE_FAILED)
                    last_m.lxm.register_delivery_callback(_cb_delivered)
                    last_m.lxm.register_failed_callback(_cb_failed)
            except Exception:
                pass

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
            "image_path": img_info["path"] if img_info else image_path,
            "image_name": img_info["name"] if img_info else (os.path.basename(image_path) if image_path else None),
            "image_size": img_info["size"] if img_info else None,
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
            for item in queued:
                try:
                    if isinstance(item, tuple):
                        content, img_p = item
                        self.send_message(dest_hex, content, image_path=img_p)
                    else:
                        self.send_message(dest_hex, item)
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

            image_info = self._extract_image_from_lxmessage(message)

            self._trigger_notification(source_hash, content, has_image=bool(image_info))

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
                "image_path": image_info["path"] if image_info else None,
                "image_name": image_info["name"] if image_info else None,
                "image_size": image_info["size"] if image_info else None,
            }

            GLib.idle_add(self._notify_message_received, msg_dict)
            GLib.idle_add(self._notify_conversations_changed)
        except Exception as e:
            RNS.log(f"Retchat: Error handling inbound message: {e}", RNS.LOG_ERROR)

    def _trigger_notification(self, sender_hex: str, content: str, has_image: bool = False):
        try:
            custom_name = self.db.get_custom_name(sender_hex)
            sender_name = custom_name or sender_hex[:8]
            if has_image:
                preview = f"📷 Bild: {content}".strip() if content else "📷 Bild empfangen"
            else:
                preview = (content[:60] + "...") if len(content) > 60 else content
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

    # --- Sync & Interfaces ---
    def request_sync(self, limit=None):
        try:
            self.app.request_lxmf_sync(limit=limit)
            RNS.log("Retchat: LXMF propagation sync requested", RNS.LOG_INFO)
        except Exception as e:
            RNS.log(f"Retchat: Sync failed: {e}", RNS.LOG_ERROR)

    def get_sync_status(self):
        try:
            return self.app.get_sync_status(), self.app.get_sync_progress()
        except Exception:
            return "unbekannt", None

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

    def request_sync(self, target_node: Optional[str] = None) -> Tuple[bool, str]:
        """Request LXMF message sync with default or specified propagation node."""
        if not self.app:
            return False, "NomadNet-Backend nicht bereit"

        try:
            if target_node:
                try:
                    target_bytes = bytes.fromhex(target_node)
                    # Check if target is a known node with propagation capability
                    for node in self.app.directory.known_nodes():
                        if getattr(node, "source_hash", None) == target_bytes:
                            self.app.message_router.set_outbound_propagation_node(target_bytes)
                            break
                except Exception:
                    pass

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

    # ------------------------------------------------------------------ #
    # Reticulum Relay Chat (RRC)
    # ------------------------------------------------------------------ #
    def _on_nomadnet_rrc_message(self, hub, msg):
        try:
            hub_hex = hub.hub_hash.hex() if hasattr(hub, "hub_hash") and hub.hub_hash else ""
            my_hash = None
            if hasattr(self.app, "rrc") and self.app.rrc and hasattr(self.app.rrc, "identity"):
                my_hash = getattr(self.app.rrc.identity, "hash", None)

            msg_dict = {
                "hub_hash": hub_hex,
                "hub_name": getattr(hub, "name", hub_hex[:8]),
                "room": getattr(msg, "room", None),
                "src": msg.src.hex() if getattr(msg, "src", None) else "",
                "nick": getattr(msg, "nick", None) or (msg.src.hex()[:8] if getattr(msg, "src", None) else "System"),
                "text": getattr(msg, "text", ""),
                "kind": getattr(msg, "kind", "msg"),
                "timestamp": getattr(msg, "ts", 0) / 1000.0,
                "is_me": (msg.src == my_hash) if (getattr(msg, "src", None) and my_hash) else False
            }
            GLib.idle_add(self._dispatch_rrc_message, msg_dict)
        except Exception as e:
            RNS.log(f"Retchat: Error dispatching RRC message: {e}", RNS.LOG_WARNING)

    def _dispatch_rrc_message(self, msg_dict: Dict[str, Any]):
        for cb in list(self._rrc_message_callbacks):
            try:
                cb(msg_dict)
            except Exception as e:
                RNS.log(f"Retchat: RRC message callback error: {e}", RNS.LOG_WARNING)

    def _on_nomadnet_rrc_change(self, hub=None):
        try:
            hub_hex = hub.hub_hash.hex() if (hub and hasattr(hub, "hub_hash") and hub.hub_hash) else None
            GLib.idle_add(self._dispatch_rrc_change, hub_hex)
        except Exception as e:
            RNS.log(f"Retchat: Error dispatching RRC change: {e}", RNS.LOG_WARNING)

    def _dispatch_rrc_change(self, hub_hex: Optional[str]):
        for cb in list(self._rrc_change_callbacks):
            try:
                cb(hub_hex)
            except Exception as e:
                RNS.log(f"Retchat: RRC change callback error: {e}", RNS.LOG_WARNING)

    def register_rrc_callbacks(self, on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
                               on_change: Optional[Callable[[Optional[str]], None]] = None):
        if on_message and on_message not in self._rrc_message_callbacks:
            self._rrc_message_callbacks.append(on_message)
        if on_change and on_change not in self._rrc_change_callbacks:
            self._rrc_change_callbacks.append(on_change)

    def get_rrc_hubs(self) -> List[Dict[str, Any]]:
        """Return list of configured RRC hubs and their rooms."""
        if not self.app or not hasattr(self.app, "rrc") or not self.app.rrc:
            return []

        results = []
        with self.app.rrc._lock:
            for hub in self.app.rrc.hubs:
                hub_hex = hub.hub_hash.hex()
                joined_rooms = sorted(list(hub.rooms))
                unread = set(hub.unread_rooms)
                results.append({
                    "hash": hub_hex,
                    "name": hub.name or f"Hub [{hub_hex[:8]}]",
                    "status": hub.status,
                    "status_text": hub.status_text,
                    "is_connected": hub.status == hub.STATUS_CONNECTED,
                    "rooms": joined_rooms,
                    "unread_rooms": list(unread),
                    "motd": getattr(hub, "motd", None),
                })
        return results

    def add_rrc_hub(self, hub_hex: str, name: Optional[str] = None, initial_room: Optional[str] = None) -> Tuple[bool, str]:
        """Add and connect to an RRC hub by destination hash."""
        if not self.app or not hasattr(self.app, "rrc") or not self.app.rrc:
            return False, "RRC-Backend nicht bereit"

        clean_hex = hub_hex.strip().lower()
        if len(clean_hex) != 32:
            return False, "Hub-Hash muss genau 32 Hex-Zeichen (16 Bytes) lang sein"

        try:
            hub_bytes = bytes.fromhex(clean_hex)
        except ValueError:
            return False, "Ungültiger Hex-Wert"

        try:
            rrc = self.app.rrc
            hub = rrc.find_hub(hub_bytes)
            if not hub:
                hub = rrc.add_hub(hub_bytes, name=name.strip() if name and name.strip() else None)

            if initial_room:
                clean_init = initial_room.strip().lower()
                if not clean_init.startswith("#"):
                    clean_init = "#" + clean_init
                hub.add_room(clean_init)

            hub.auto_reconnect = True
            rrc.save()

            hub.connect()
            return True, f"Hub '{hub.name}' hinzugefügt, verbinde..."
        except Exception as e:
            return False, f"Fehler beim Hinzufügen des Hubs: {e}"

    def remove_rrc_hub(self, hub_hex: str) -> bool:
        """Disconnect and remove an RRC hub."""
        hub = self._find_hub_by_hex(hub_hex)
        if not hub or not self.app or not hasattr(self.app, "rrc"):
            return False

        try:
            try:
                hub.disconnect()
            except Exception:
                pass
            self.app.rrc.remove_hub(hub)
            return True
        except Exception as e:
            RNS.log(f"Retchat: Error removing hub: {e}", RNS.LOG_WARNING)
            return False

    def connect_rrc_hub(self, hub_hex: str):
        hub = self._find_hub_by_hex(hub_hex)
        if hub:
            hub.connect()

    def disconnect_rrc_hub(self, hub_hex: str):
        hub = self._find_hub_by_hex(hub_hex)
        if hub:
            hub.disconnect()

    def join_rrc_room(self, hub_hex: str, room_name: str) -> bool:
        hub = self._find_hub_by_hex(hub_hex)
        if not hub:
            return False
        clean_room = room_name.strip().lower()
        if not clean_room.startswith("#"):
            clean_room = "#" + clean_room
        try:
            hub.add_room(clean_room)
            if hub.status == hub.STATUS_CONNECTED:
                hub.join_room(clean_room)
            self.app.rrc.save()
            self.app.rrc._notify_change(hub)
            return True
        except Exception as e:
            RNS.log(f"Retchat: Error joining room: {e}", RNS.LOG_WARNING)
            return False

    def part_rrc_room(self, hub_hex: str, room_name: str) -> bool:
        hub = self._find_hub_by_hex(hub_hex)
        if not hub:
            return False
        try:
            hub.part_room(room_name)
            return True
        except Exception as e:
            RNS.log(f"Retchat: Error parting room: {e}", RNS.LOG_WARNING)
            return False

    def send_rrc_message(self, hub_hex: str, room_name: str, text: str) -> Tuple[bool, str]:
        hub = self._find_hub_by_hex(hub_hex)
        if not hub:
            return False, "Hub nicht gefunden"
        if hub.status != hub.STATUS_CONNECTED:
            return False, f"Hub ist nicht verbunden ({hub.status_text})"

        text_str = text.strip()
        if not text_str:
            return False, "Nachricht darf nicht leer sein"

        try:
            if text_str.startswith("/me "):
                action_text = text_str[4:].strip()
                hub.send_action(room_name, action_text)
            elif text_str.startswith("/part"):
                hub.part_room(room_name)
            elif text_str.startswith("/join "):
                new_room = text_str[6:].strip()
                self.join_rrc_room(hub_hex, new_room)
            else:
                hub.send_message(room_name, text_str)
            return True, "Gesendet"
        except Exception as e:
            return False, f"Fehler beim Senden: {e}"

    def get_rrc_messages(self, hub_hex: str, room_name: str) -> List[Dict[str, Any]]:
        hub = self._find_hub_by_hex(hub_hex)
        if not hub:
            return []
        try:
            raw_msgs = hub.get_messages(room_name)
            my_hash = None
            if hasattr(self.app, "rrc") and self.app.rrc and hasattr(self.app.rrc, "identity"):
                my_hash = getattr(self.app.rrc.identity, "hash", None)

            results = []
            for m in raw_msgs:
                results.append({
                    "kind": getattr(m, "kind", "msg"),
                    "room": getattr(m, "room", room_name),
                    "src": m.src.hex() if getattr(m, "src", None) else "",
                    "nick": getattr(m, "nick", None) or (m.src.hex()[:8] if getattr(m, "src", None) else "System"),
                    "text": getattr(m, "text", ""),
                    "timestamp": getattr(m, "ts", 0) / 1000.0,
                    "is_me": (m.src == my_hash) if (getattr(m, "src", None) and my_hash) else False
                })
            return results
        except Exception:
            return []

    def get_rrc_members(self, hub_hex: str, room_name: str) -> List[Dict[str, str]]:
        hub = self._find_hub_by_hex(hub_hex)
        if not hub:
            return []
        try:
            members = hub.get_members(room_name)
            results = []
            for m in members:
                m_bytes = bytes(m) if isinstance(m, (bytes, bytearray)) else None
                m_hex = m_bytes.hex() if m_bytes else str(m)
                nick = hub.display_name_for(m_bytes) if m_bytes else m_hex[:8]
                results.append({"hash": m_hex, "nick": nick})
            return results
        except Exception:
            return []

    def mark_rrc_room_read(self, hub_hex: str, room_name: str):
        hub = self._find_hub_by_hex(hub_hex)
        if hub:
            try:
                hub.mark_read(room_name)
            except Exception:
                pass

    def _find_hub_by_hex(self, hub_hex: str):
        if not self.app or not hasattr(self.app, "rrc") or not self.app.rrc:
            return None
        try:
            hub_bytes = bytes.fromhex(hub_hex.strip().lower())
            return self.app.rrc.find_hub(hub_bytes)
        except Exception:
            return None

    def shutdown(self):
        """Clean shutdown of background threads and NomadNet."""
        self._poll_stop = True
        try:
            self.app.exit_handler()
        except Exception as e:
            RNS.log(f"Retchat: Error exiting NomadNet: {e}", RNS.LOG_ERROR)
