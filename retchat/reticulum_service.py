"""Reticulum and NomadNet LXMF Service integration for Retchat."""

import importlib.metadata
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
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

from retchat.contact_uri import Contact, contact_uri
from retchat.database import Database
from retchat.nomadnet_pages import PageFetcher

# Message states mapped for UI
STATE_SENDING = 0
STATE_SENT = 1
STATE_DELIVERED = 2
STATE_FAILED = 3

def library_versions() -> Dict[str, str]:
    """Installed versions of the Reticulum stack (pinned in requirements.txt)."""
    versions = {}
    for name in ("rns", "lxmf", "nomadnet"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unknown"
    return versions


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

# Seconds between saves of LXMF's outbound stamp costs (see _throttle_stamp_cost_saves)
STAMP_COST_SAVE_INTERVAL = 60.0
# Seconds announces are collected before the UI is told about them
ANNOUNCE_BATCH_INTERVAL = 5.0
# Seconds after the last "conversations changed" event before the UI reloads
CONVERSATIONS_CHANGED_DELAY = 1.0
# Seconds to wait for RNS to attach/detach an interface (see _apply_interface_change)
INTERFACE_APPLY_TIMEOUT = 20.0


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


# --- Reactions ------------------------------------------------------------------
#
# A reaction is a separate LXMF message without text, carrying
#   fields[FIELD_REACTION] = {REACTION_TO: <target LXMessage.hash>,
#                             REACTION_CONTENT: <emoji, UTF-8>}
# (LXMF standard, also sent by Columba and MeshChatX). The reacting user is
# the (signed) source of that message. Reactions can't be withdrawn; every
# sender has each emoji at most once per message.
# Older Columba/MeshChatX versions used fields[0x10] = {"reaction_to": hex,
# "emoji": str, "sender": hex}; it is accepted on receipt, but "sender" is
# ignored in favour of the authenticated message source.
FIELD_REACTION = getattr(LXMF, "FIELD_REACTION", 0x40)
REACTION_TO = getattr(LXMF, "REACTION_TO", 0x00)
REACTION_CONTENT = getattr(LXMF, "REACTION_CONTENT", 0x01)
FIELD_REACTION_LEGACY = 0x10
# An emoji is 1-10 code points (flags, skin tones, ZWJ sequences); allow some
# room for short text reactions, but nothing that would break the layout.
MAX_REACTION_LENGTH = 16


def _message_hash_hex(value: Any) -> Optional[str]:
    """A full LXMF message hash (bytes or hex string) as lowercase hex."""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).hex() if len(value) == 32 else value.decode("ascii", errors="replace")
    if isinstance(value, str):
        value = value.strip().lower()
        if _HEX_MSG_RE.fullmatch(value):
            return value
    return None


def sanitize_reaction(value: Any) -> Optional[str]:
    """Reaction content as displayable text, or None if it is unusable."""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_REACTION_LENGTH:
        return None
    # Control characters and line breaks; format characters (ZWJ, tags of
    # subdivision flags) and variation selectors are part of emoji.
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp", "Cs", "Co", "Cn") for c in value):
        return None
    return value


def _get_key(d: dict, key: str) -> Any:
    value = d.get(key)
    return value if value is not None else d.get(key.encode())


def parse_reaction(fields: Any) -> Optional[Tuple[str, Optional[str]]]:
    """(target message hash, emoji) if ``fields`` mark a reaction message.

    The emoji is None if the target is valid but the content is not; such a
    message is still a reaction (and not shown as a message), it's just not
    displayed.
    """
    if not isinstance(fields, dict):
        return None
    data = fields.get(FIELD_REACTION)
    if isinstance(data, dict):
        target = _message_hash_hex(data.get(REACTION_TO))
        if target:
            return target, sanitize_reaction(data.get(REACTION_CONTENT))
    legacy = fields.get(FIELD_REACTION_LEGACY)
    if isinstance(legacy, dict):
        target = _message_hash_hex(_get_key(legacy, "reaction_to"))
        if target:
            return target, sanitize_reaction(_get_key(legacy, "emoji"))
    return None


def reaction_fields(target_hash: str, emoji: str) -> Dict[int, Any]:
    return {FIELD_REACTION: {REACTION_TO: bytes.fromhex(target_hash), REACTION_CONTENT: emoji.encode("utf-8")}}


# --- Replies ----------------------------------------------------------------------
#
# A reply is a normal message with
#   fields[FIELD_REPLY_TO]    = <full LXMessage.hash of the message replied to>
#   fields[FIELD_REPLY_QUOTE] = <quoted text, UTF-8>
# (LXMF standard, also used by MeshChatX). The quote lets clients show what
# was replied to without having the original. Older Columba versions put
# "reply_to" (hex) into their app extensions dict, fields[0x10]; accepted on
# receipt only.
FIELD_REPLY_TO = getattr(LXMF, "FIELD_REPLY_TO", 0x30)
FIELD_REPLY_QUOTE = getattr(LXMF, "FIELD_REPLY_QUOTE", 0x31)
# Characters of the original sent as quote: short, so that a short reply
# still fits into a single packet.
MAX_REPLY_QUOTE = 140
# Received quotes are shown in two lines at most; keep no more than this.
MAX_SHOWN_QUOTE = 300


def _clean_quote(value: Any, limit: int) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return ""
    # Control characters except line breaks
    value = "".join(c for c in value if c in "\n\t" or unicodedata.category(c) != "Cc").strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def parse_reply(fields: Any) -> Optional[Tuple[str, str]]:
    """(target message hash, quote) if ``fields`` mark a reply; the quote may be empty."""
    if not isinstance(fields, dict):
        return None
    target = _message_hash_hex(fields.get(FIELD_REPLY_TO))
    if target:
        return target, _clean_quote(fields.get(FIELD_REPLY_QUOTE), MAX_SHOWN_QUOTE)
    legacy = fields.get(FIELD_REACTION_LEGACY)
    if isinstance(legacy, dict):
        target = _message_hash_hex(_get_key(legacy, "reply_to"))
        if target:
            return target, ""
    return None


def reply_fields(target_hash: str, quote: str) -> Dict[int, Any]:
    fields: Dict[int, Any] = {FIELD_REPLY_TO: bytes.fromhex(target_hash)}
    quote = _clean_quote(quote, MAX_REPLY_QUOTE)
    if quote:
        fields[FIELD_REPLY_QUOTE] = quote.encode("utf-8")
    return fields


def summarize_reactions(rows: List[Dict[str, Any]], own_hash: str) -> Dict[str, List[Dict[str, Any]]]:
    """Reaction rows (oldest first) grouped per target message.

    Returns {target_hash: [{"emoji", "count", "mine"}]} in the order the
    emoji were first used; every sender counts once per emoji.
    """
    senders: Dict[str, Dict[str, set]] = {}
    for row in rows:
        per_emoji = senders.setdefault(row["target_hash"], {})
        per_emoji.setdefault(row["emoji"], set()).add(row["sender_hash"])
    return {
        target: [{"emoji": emoji, "count": len(s), "mine": own_hash in s} for emoji, s in per_emoji.items()]
        for target, per_emoji in senders.items()
    }


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
        # Reactions are registered before NomadNet stores them, so a chat
        # list refresh triggered by the storing already hides them.
        reaction = None
        try:
            reaction = self._service._register_inbound_reaction(message)
        except Exception as e:
            RNS.log(f"Retchat: Error handling reaction: {e}", RNS.LOG_ERROR)
        unread_before = Conversation.unread_conversations.get(message.source_hash)

        super().lxmf_delivery(message)

        try:
            if reaction is not None:
                # NomadNet counts every stored message as unread; a reaction
                # adds no message to the chat, so keep the previous count.
                self._service._restore_unread(message.source_hash, unread_before)
                self._service._on_inbound_reaction(message, *reaction)
            else:
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
        self._announce_callbacks: List[Callable[[List[Dict[str, Any]]], None]] = []
        self._path_resolved_callbacks: List[Callable[[str, int], None]] = []
        self._conversations_changed_callbacks: List[Callable[[], None]] = []
        self._reaction_callbacks: List[Callable[[str, str, List[Dict[str, Any]]], None]] = []

        # Hashes of reaction messages, hidden in the chat (see _is_reaction_message).
        self._reaction_hashes: set = self.db.get_reaction_hashes()
        # Messages without text and attachments that were checked and aren't reactions.
        self._plain_empty_hashes: set = set()
        # Names of NomadNet nodes from their announces (hex hash -> name)
        self._node_names: Dict[str, str] = {}
        self._page_fetcher: Optional[PageFetcher] = None
        # See _throttle_stamp_cost_saves
        self._stamp_cost_router = None
        self._stamp_costs_dirty = False
        self._stamp_costs_saved_at = time.monotonic()
        # Announces waiting for the next batch (see _dispatch_directory_announce)
        self._announce_lock = threading.Lock()
        self._announce_batch: Dict[str, Dict[str, Any]] = {}
        self._announce_flush_scheduled = False
        # See _schedule_conversations_changed
        self._conv_changed_lock = threading.Lock()
        self._conv_changed_scheduled = False

        # Ensure ~/.reticulum/config has TCP enabled as default, and enable_transport=False
        self._ensure_tcp_default_config()

        # Install quiet UI monkeypatch before creating NomadNetworkApp
        install_quiet_ui()

        Conversation.created_callback = self._on_conversations_changed_nomadnet
        install_delivery_hook(self._on_outbound_state)
        self.app = _RetchatApp(self, configdir=self._configdir, rnsconfigdir=self._rnsconfigdir)

        self._conv_cache: Dict[str, Conversation] = {}
        # dest hash -> queued (content, attachment path, reply-to hash, temporary message hash)
        self._pending: Dict[str, List[Tuple[str, Optional[str], Optional[str], str]]] = {}

        # Hook announce logger on app.directory to dispatch announces without extra overhead
        self._hook_directory_announces()
        self._throttle_stamp_cost_saves()

        # Periodic background worker for flushing pending messages
        self._poll_stop = False
        self._poll_thread = threading.Thread(target=self._background_worker, daemon=True, name="Retchat-Worker")
        self._poll_thread.start()

        RNS.log(
            f"Retchat: ReticulumService initialised with NomadNet backend "
            f"({', '.join(f'{n} {v}' for n, v in library_versions().items())}). "
            f"Identity: {self.identity_hex}, Destination: {self.delivery_destination_hex}",
            RNS.LOG_NOTICE
        )

    def _config_path(self) -> str:
        """The Reticulum config file this instance uses.

        Same lookup as RNS.Reticulum, which also reads interface sections
        from it when attaching one at runtime; before RNS is started (see
        _ensure_tcp_default_config) the lookup is repeated here.
        """
        if self._rnsconfigdir:
            return os.path.join(self._rnsconfigdir, "config")
        if RNS.Reticulum.configpath:
            return RNS.Reticulum.configpath
        home = os.path.expanduser("~")
        for config_dir in ("/etc/reticulum", os.path.join(home, ".config", "reticulum")):
            if os.path.isdir(config_dir) and os.path.isfile(os.path.join(config_dir, "config")):
                return os.path.join(config_dir, "config")
        return os.path.join(home, ".reticulum", "config")

    def _ensure_tcp_default_config(self):
        """Ensure ~/.reticulum/config exists with TCP enabled and transport disabled for mobile phones."""
        try:
            cfg_path = self._config_path()
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)

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
        cfg_path = self._config_path()

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

    def save_tcp_settings(self, host: str, port: str, disable_auto: Optional[bool] = None,
                          on_applied: Optional[Callable[[str, str], None]] = None) -> str:
        """Save the TCP hub (and optionally the AutoInterface state) in the
        Reticulum config and reconnect the TCP interface right away.

        ``disable_auto`` None leaves the AutoInterface as it is.
        ``on_applied(name, outcome)`` as for set_interface_enabled.
        """
        cfg_path = self._config_path()

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
        cfg["interfaces"][tcp_key]["interface_enabled"] = "true"
        cfg["interfaces"][tcp_key]["target_host"] = host.strip()
        cfg["interfaces"][tcp_key]["target_port"] = port.strip()

        if disable_auto is not None:
            if "Default Interface" in cfg["interfaces"]:
                cfg["interfaces"]["Default Interface"]["enabled"] = "no" if disable_auto else "yes"
            elif disable_auto:
                cfg["interfaces"]["Default Interface"] = {
                    "type": "AutoInterface",
                    "enabled": "no"
                }

        cfg.write()
        # Reconnect with the new target (or start it, if it wasn't running)
        self._apply_interface_change(tcp_key, "reload", on_applied)
        return cfg_path

    # --- LXMF stamp costs ---
    def _throttle_stamp_cost_saves(self):
        """Save LXMF's outbound stamp costs at most every STAMP_COST_SAVE_INTERVAL.

        LXMRouter.update_stamp_cost runs for every LXMF announce that carries
        a stamp cost and starts a thread that packs the *whole* table (one
        entry per announcing peer, kept for 45 days: thousands of entries)
        with the pure-Python msgpack and rewrites the file. On a busy network
        that is several 300+ KB rewrites per second for a one-entry change.
        Here the table is only updated in memory; the background worker
        saves it periodically and shutdown() saves it at exit (LXMRouter's
        own exit handler doesn't). Costs lost in a crash are learned again
        from the next announce.
        """
        router = getattr(self.app, "message_router", None)
        if router is None or not hasattr(router, "outbound_stamp_costs") \
                or not hasattr(router, "save_outbound_stamp_costs"):
            return

        def update_stamp_cost(destination_hash, stamp_cost):
            router.outbound_stamp_costs[destination_hash] = [time.time(), stamp_cost]
            self._stamp_costs_dirty = True

        router.update_stamp_cost = update_stamp_cost
        self._stamp_cost_router = router

    def _save_stamp_costs(self, force: bool = False):
        router = self._stamp_cost_router
        if router is None or not self._stamp_costs_dirty:
            return
        now = time.monotonic()
        if not force and now - self._stamp_costs_saved_at < STAMP_COST_SAVE_INTERVAL:
            return
        # Cleared before saving: an update during the save marks it again.
        self._stamp_costs_dirty = False
        self._stamp_costs_saved_at = now
        router.save_outbound_stamp_costs()

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
                "kind": kind,
                "aspect": f"nomadnet.{kind}",
                "receiving_interface": "",
                "last_seen": time.time()
            }
            if kind == "node" and name:
                self._node_names[dest_hex] = name
            # Announces aren't needed in real time; collect them (newest per
            # destination wins) and hand them to the UI in batches.
            with self._announce_lock:
                self._announce_batch[dest_hex] = data
                schedule = not self._announce_flush_scheduled
                self._announce_flush_scheduled = True
            if schedule:
                GLib.timeout_add(int(ANNOUNCE_BATCH_INTERVAL * 1000), self._flush_announces)
        except Exception as e:
            RNS.log(f"Retchat: Error dispatching announce: {e}", RNS.LOG_DEBUG)

    def _flush_announces(self) -> bool:
        with self._announce_lock:
            batch = list(self._announce_batch.values())
            self._announce_batch.clear()
            self._announce_flush_scheduled = False
        if batch:
            batch.sort(key=lambda d: d["last_seen"])
            self._notify_announces(batch)
        return False

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
    def contact_uri(self) -> str:
        """Own address with public key, as lxma:// link (shown as QR code)."""
        return contact_uri(self.delivery_destination_hex, self.identity.get_public_key())

    def learn_contact(self, contact: Contact) -> bool:
        """Remember the identity of a scanned contact link.

        Messages to it can then be encrypted right away, without waiting
        for an announce. ``contact.public_key`` was checked against the
        address by parse_contact. An already known destination is left
        alone (remembering again would drop its announced name). Returns
        whether the identity is known afterwards.
        """
        dest = bytes.fromhex(contact.destination_hash)
        if RNS.Identity.recall(dest) is not None:
            return True
        if contact.public_key is None:
            return False
        RNS.Identity.remember(None, dest, contact.public_key)
        RNS.log(f"Retchat: Learned identity of {contact.destination_hash} from a contact link", RNS.LOG_INFO)
        return True

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

    def _conversation_summary(self, source_hash: str, display_name: Optional[str], unread: int,
                              activity: float, custom_name: Optional[str]) -> Dict[str, Any]:
        """Chat list entry of an existing conversation (``source_hash`` lowercase)."""
        dn = display_name if (display_name and display_name != "Undefined") else None

        conv = self.conversation(source_hash)
        last_text = ""
        last_time = activity if activity else 0.0

        last_m = None
        for m in sorted(conv.messages, key=lambda m: m.sort_timestamp, reverse=True):
            if not self._is_reaction_message(m, source_hash):
                last_m = m
                break
            _release(m)
        if last_m is not None:
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
            h = RNS.Transport.hops_to(bytes.fromhex(source_hash))
            if h != RNS.Transport.PATHFINDER_M:
                hops = h
        except Exception:
            pass

        return {
            "destination_hash": source_hash,
            "display_name": dn,
            "custom_name": custom_name,
            "last_message_text": last_text,
            "last_message_time": last_time,
            "unread_count": unread,
            "hops": hops,
        }

    def get_conversations(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        raw_list = Conversation.conversation_list(self.app)
        custom_names = self.db.get_all_custom_names()
        results = []
        q = query.strip().lower() if query else None

        for entry in raw_list:
            try:
                source_hash, display_name, trust, sort_name, unread, activity, failed = entry[:7]
                source_hash = source_hash.lower()
                c = self._conversation_summary(source_hash, display_name, unread, activity,
                                               custom_names.get(source_hash))
                if q:
                    texts = (source_hash, c["custom_name"], c["display_name"], c["last_message_text"])
                    if not any(t and q in t.lower() for t in texts):
                        continue
                results.append(c)
            except Exception as e:
                RNS.log(f"Retchat: Error loading conversation entry {entry}: {e}", RNS.LOG_ERROR)

        results.sort(key=lambda c: c.get("last_message_time", 0.0), reverse=True)
        return results

    def _conversation_unread(self, source_hash: bytes, conv_dir: str) -> int:
        # As in NomadNet's Conversation.conversation_list
        if source_hash in Conversation.unread_conversations:
            return Conversation.unread_conversations[source_hash]
        unread_path = os.path.join(conv_dir, "unread")
        if not os.path.isfile(unread_path):
            return 0
        try:
            with open(unread_path, "r") as uf:
                content = uf.read().strip()
                unread = int(content) if content else 1
        except Exception:
            unread = 1
        Conversation.unread_conversations[source_hash] = unread
        return unread

    def get_conversation(self, dest_hex: str) -> Optional[Dict[str, Any]]:
        """Chat list entry of one chat, without building the whole list."""
        dest_hex = dest_hex.strip().lower()
        conv_dir = os.path.join(self.app.conversationpath, dest_hex)
        if _HEX_DEST_RE.fullmatch(dest_hex) and os.path.isdir(conv_dir):
            try:
                source_hash = bytes.fromhex(dest_hex)
                display_name = self.app.directory.display_name(source_hash)
                if display_name is None:
                    app_data = RNS.Identity.recall_app_data(source_hash)
                    if app_data:
                        display_name = LXMF.display_name_from_app_data(app_data)
                try:
                    activity = os.path.getmtime(conv_dir)
                except OSError:
                    activity = 0
                return self._conversation_summary(dest_hex, display_name,
                                                  self._conversation_unread(source_hash, conv_dir),
                                                  activity, self.db.get_custom_name(dest_hex))
            except Exception as e:
                RNS.log(f"Retchat: Error loading conversation {dest_hex}: {e}", RNS.LOG_ERROR)
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
                if self._is_reaction_message(m, dest_hex):
                    continue
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
                # Fields aren't in NomadNet's index, this reads the message
                # file (~0.02 ms per message; released below).
                reply = parse_reply(m.get_fields())

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
                    "reply_to": reply[0] if reply else None,
                    "reply_quote": reply[1] if reply else "",
                    **info,
                })
            except Exception as e:
                RNS.log(f"Retchat: Error loading message: {e}", RNS.LOG_ERROR)
            finally:
                _release(m)

        reactions = summarize_reactions(self.db.get_reactions(dest_hex), own_hash)
        for msg in out:
            msg["reactions"] = reactions.get(msg["message_hash"], [])
        return out

    # --- Reactions ---------------------------------------------------------------

    def get_reactions(self, dest_hex: str, message_hash: str) -> List[Dict[str, Any]]:
        """Reactions of one message: [{"emoji", "count", "mine"}]."""
        dest_hex = dest_hex.strip().lower()
        rows = self.db.get_reactions(dest_hex, message_hash)
        return summarize_reactions(rows, self.delivery_destination_hex.lower()).get(message_hash.lower(), [])

    def send_reaction(self, dest_hex: str, message_hash: str, emoji: str) -> List[Dict[str, Any]]:
        """React to a received message; returns the message's reactions afterwards.

        Reacting twice with the same emoji sends nothing (reactions can't
        be withdrawn, so there is nothing to toggle).
        """
        dest_hex = dest_hex.strip().lower()
        message_hash = message_hash.strip().lower()
        if not _HEX_DEST_RE.fullmatch(dest_hex):
            raise ValueError("Ungültige Zieladresse")
        if not _HEX_MSG_RE.fullmatch(message_hash):
            raise ValueError("Auf diese Nachricht kann nicht reagiert werden")
        clean = sanitize_reaction(emoji)
        if clean is None:
            raise ValueError("Ungültige Reaktion")

        own_hash = self.delivery_destination_hex.lower()
        if self.db.has_reaction(dest_hex, message_hash, own_hash, clean):
            return self.get_reactions(dest_hex, message_hash)
        if not self.peer_known(dest_hex):
            Conversation.query_for_peer(dest_hex)
            raise ValueError("Kontakt ist noch nicht bekannt, bitte später erneut versuchen")

        conv = self.conversation(dest_hex)
        if not conv.send(content="", fields=reaction_fields(message_hash, clean)) or not conv.messages:
            raise ValueError("Reaktion konnte nicht gesendet werden")
        sent_m = conv.messages[-1]
        reaction_hash = sent_m.get_hash().hex() if sent_m.get_hash() else f"local_{os.urandom(16).hex()}"
        _release(sent_m)

        self._reaction_hashes.add(reaction_hash)
        self.db.add_reaction(reaction_hash, dest_hex, message_hash, own_hash, clean, time.time())
        return self.get_reactions(dest_hex, message_hash)

    def _is_reaction_message(self, m: ConversationMessage, conversation_hash: str) -> bool:
        """Whether a stored message is a reaction (hidden in chat and preview).

        Reactions received while Retchat runs are registered on receipt.
        Others (received before Retchat supported them) have no text and no
        attachments, so only such messages are read from disk and checked.
        """
        h = m.get_hash()
        msg_hash = h.hex() if h else None
        if msg_hash is None or msg_hash in self._plain_empty_hashes:
            return False
        if msg_hash in self._reaction_hashes:
            return True
        try:
            if (m.get_content() or "").strip() or m.has_attachments():
                return False
            parsed = parse_reaction(m.get_fields())
        except Exception:
            return False
        if parsed is None:
            self._plain_empty_hashes.add(msg_hash)
            return False

        target, emoji = parsed
        src = _source_hash(m)
        sender = src.hex().lower() if src else conversation_hash
        self._reaction_hashes.add(msg_hash)
        self.db.add_reaction(msg_hash, conversation_hash, target, sender, emoji or "",
                             m.get_timestamp() or m.sort_timestamp)
        return True

    def _register_inbound_reaction(self, message: LXMF.LXMessage) -> Optional[Tuple[str, Optional[str]]]:
        """Store a received reaction message; (target hash, emoji) or None (Reticulum thread)."""
        parsed = parse_reaction(message.fields)
        if parsed is None or message.hash is None:
            return None
        target, emoji = parsed
        source_hash = message.source_hash.hex().lower()
        msg_hash = message.hash.hex()
        self._reaction_hashes.add(msg_hash)
        self.db.add_reaction(msg_hash, source_hash, target, source_hash, emoji or "",
                             message.timestamp or time.time())
        return parsed

    def _restore_unread(self, source_hash: bytes, unread_before: Optional[int]):
        """Reset NomadNet's unread counter of a conversation to ``unread_before``."""
        if unread_before is None:
            # mark_conversation_read would also drop the failed marker
            Conversation.unread_conversations.pop(source_hash, None)
            path = os.path.join(self.app.conversationpath, source_hash.hex(), "unread")
            try:
                os.unlink(path)
            except OSError:
                pass
        else:
            Conversation.unread_conversations[source_hash] = unread_before
            try:
                with open(os.path.join(self.app.conversationpath, source_hash.hex(), "unread"), "w") as f:
                    f.write(str(unread_before))
            except OSError:
                pass

    def _on_inbound_reaction(self, message: LXMF.LXMessage, target_hash: str, emoji: Optional[str]):
        """A reaction was received and stored (Reticulum thread)."""
        if emoji is not None:
            # Conversations are read and rescanned on the main loop only.
            GLib.idle_add(self._announce_reaction, message.source_hash.hex().lower(), target_hash, emoji)

    def _announce_reaction(self, source_hash: str, target_hash: str, emoji: str) -> bool:
        preview = self._message_preview(source_hash, target_hash)
        self._trigger_notification(source_hash, f"{emoji} zu: {preview or 'deiner Nachricht'}",
                                   title_prefix="Reaktion von")
        self._notify_reaction(source_hash, target_hash, self.get_reactions(source_hash, target_hash))
        return False

    def _message_preview(self, conversation_hash: str, message_hash: str) -> str:
        conv = self.conversation(conversation_hash)
        for m in conv.messages:
            h = m.get_hash()
            if h and h.hex() == message_hash:
                try:
                    return (m.get_content() or "").strip()
                except Exception:
                    return ""
                finally:
                    _release(m)
        return ""

    def send_message(self, dest_hex: str, content: str, attachment_path: Optional[str] = None,
                     reply_to: Optional[str] = None) -> Dict[str, Any]:
        """Send a message, optionally with an image or any other file (see
        _prepare_attachment) and as reply to the message ``reply_to`` (hash)."""
        dest_hex = dest_hex.strip().lower()
        if len(dest_hex) != 32:
            raise ValueError("Ungültige Zieladresse: Muss ein 32-Zeichen Hex-Hash sein.")
        if reply_to is not None:
            reply_to = reply_to.strip().lower()
            if not _HEX_MSG_RE.fullmatch(reply_to):
                raise ValueError("Auf diese Nachricht kann nicht geantwortet werden")

        conv = self.conversation(dest_hex)
        now = time.time()

        fields = None
        info = self._attachment_info([])
        if attachment_path:
            fields, info = self._prepare_attachment(attachment_path)
        reply_quote = ""
        if reply_to is not None:
            reply_quote = _clean_quote(self._reply_quote(dest_hex, reply_to), MAX_REPLY_QUOTE)
            fields = {**(fields or {}), **reply_fields(reply_to, reply_quote)}

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
            self._pending.setdefault(dest_hex, []).append((content, attachment_path, reply_to, msg_hash))
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
            "reply_to": reply_to,
            "reply_quote": reply_quote,
            **info,
        }

    def _reply_quote(self, conversation_hash: str, message_hash: str) -> str:
        """Text of a message as quoted in a reply (attachments as in the chat list)."""
        for m in self.conversation(conversation_hash).messages:
            h = m.get_hash()
            if h and h.hex() == message_hash:
                try:
                    text = (m.get_content() or "").strip()
                    return self._preview_text(text, self._attachment_info(self._attachments(message_hash, m)))
                except Exception:
                    return ""
                finally:
                    _release(m)
        return ""

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
            for content, img_p, reply_to, temp_hash in queued:
                try:
                    result = self.send_message(dest_hex, content, attachment_path=img_p, reply_to=reply_to)
                    if result["message_hash"] != temp_hash:
                        GLib.idle_add(self._notify_message_id, temp_hash, result["message_hash"])
                except Exception as e:
                    RNS.log(f"Retchat: Failed to send pending message: {e}", RNS.LOG_ERROR)
            sent.append(dest_hex)
            self._schedule_conversations_changed()
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
            try:
                self._save_stamp_costs()
            except Exception as e:
                RNS.log(f"Retchat: Error saving stamp costs: {e}", RNS.LOG_ERROR)

    # --- Announces & Path Finding ---
    def get_announces(self, query: Optional[str] = None) -> List[Dict[str, Any]]:
        # NomadNet's stream is its node, peer and propagation node lists after
        # each other; show the newest first.
        stream = sorted(getattr(self.app.directory, "announce_stream", []),
                        key=lambda item: item[0] or 0, reverse=True)
        seen = set()
        results = []
        q = query.strip().lower() if query else None

        for item in stream:
            try:
                timestamp, source_hash, app_data, kind = item[:4]
                dest_hex = source_hash.hex() if isinstance(source_hash, bytes) else str(source_hash).lower()

                # Propagation nodes (store-and-forward servers) are neither
                # contacts nor pages; their app data is binary.
                if kind not in ("peer", "node") or dest_hex in seen:
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

                if kind == "node" and display_name:
                    self._node_names[dest_hex] = display_name
                results.append({
                    "destination_hash": dest_hex,
                    "display_name": display_name,
                    "hops": hops,
                    "kind": kind,
                    "aspect": f"nomadnet.{kind}",
                    "receiving_interface": "",
                    "last_seen": timestamp
                })
            except Exception as e:
                RNS.log(f"Retchat: Error parsing announce stream item: {e}", RNS.LOG_DEBUG)

        return results

    # --- NomadNet nodes (pages) -------------------------------------------------------

    @property
    def page_fetcher(self) -> PageFetcher:
        if self._page_fetcher is None:
            self._page_fetcher = PageFetcher(self.app, GLib.idle_add)
        return self._page_fetcher

    @staticmethod
    def _identity_hash_for(dest_hex: str, aspect: str) -> Optional[str]:
        """Hash of the ``aspect`` destination with the same identity as ``dest_hex``."""
        try:
            identity = RNS.Identity.recall(bytes.fromhex(dest_hex))
        except ValueError:
            return None
        if identity is None:
            return None
        return RNS.Destination.hash_from_name_and_identity(aspect, identity).hex()

    def is_node(self, dest_hex: str) -> bool:
        """Whether ``dest_hex`` is a NomadNet node (serves pages) rather than a contact."""
        dest_hex = dest_hex.strip().lower()
        if dest_hex in self._node_names:
            return True
        return self._identity_hash_for(dest_hex, "nomadnetwork.node") == dest_hex

    def node_name(self, dest_hex: str) -> Optional[str]:
        """Name of a node from its announce (also one heard in an earlier session)."""
        dest_hex = dest_hex.strip().lower()
        name = self._node_names.get(dest_hex)
        if name is None:
            try:
                # Nodes announce their name as plain UTF-8; RNS keeps the last app data.
                app_data = RNS.Identity.recall_app_data(bytes.fromhex(dest_hex))
                if app_data:
                    name = app_data.decode("utf-8", errors="replace").strip() or None
            except Exception:
                name = None
            if name:
                self._node_names[dest_hex] = name
        return name

    @property
    def own_node_hex(self) -> Optional[str]:
        node = getattr(self.app, "node", None)
        return node.destination.hash.hex() if node is not None else None

    def operator_address(self, node_hex: str) -> Optional[str]:
        """LXMF address of the operator of a node (both belong to the same identity)."""
        return self._identity_hash_for(node_hex.strip().lower(), "lxmf.delivery")

    def chat_address(self, dest_hex: str) -> str:
        """Address to chat with: a node's operator instead of the node itself.

        A conversation with a node hash would send to the operator, but store
        the messages under a different hash, leaving an empty chat.
        """
        dest_hex = dest_hex.strip().lower()
        if self.is_node(dest_hex):
            return self.operator_address(dest_hex) or dest_hex
        return dest_hex

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
            reply = parse_reply(message.fields)

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
                "reply_to": reply[0] if reply else None,
                "reply_quote": reply[1] if reply else "",
                **info,
            }

            GLib.idle_add(self._notify_message_received, msg_dict)
            self._schedule_conversations_changed()
        except Exception as e:
            RNS.log(f"Retchat: Error handling inbound message: {e}", RNS.LOG_ERROR)

    def _trigger_notification(self, sender_hex: str, preview: str, title_prefix: str = "Nachricht von"):
        try:
            custom_name = self.db.get_custom_name(sender_hex)
            sender_name = custom_name or sender_hex[:8]
            preview = (preview[:60] + "...") if len(preview) > 60 else preview
            subprocess.Popen([
                "notify-send",
                "-a", "Retchat",
                "-i", "mail-message-new-symbolic",
                f"{title_prefix} {sender_name}",
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

    def add_announce_callback(self, cb: Callable[[List[Dict[str, Any]]], None]):
        """cb(announces): announces received since the last call, oldest first,
        one per destination; called at most every ANNOUNCE_BATCH_INTERVAL."""
        self._announce_callbacks.append(cb)

    def add_path_resolved_callback(self, cb: Callable[[str, int], None]):
        self._path_resolved_callbacks.append(cb)

    def add_conversations_changed_callback(self, cb: Callable[[], None]):
        self._conversations_changed_callbacks.append(cb)

    def add_reaction_callback(self, cb: Callable[[str, str, List[Dict[str, Any]]], None]):
        """cb(conversation_hash, message_hash, reactions): a reaction to a message was received."""
        self._reaction_callbacks.append(cb)

    def _notify_reaction(self, conversation_hash: str, message_hash: str, reactions: List[Dict[str, Any]]) -> bool:
        for cb in self._reaction_callbacks:
            try:
                cb(conversation_hash, message_hash, reactions)
            except Exception as e:
                RNS.log(f"Retchat: Error in reaction callback: {e}", RNS.LOG_ERROR)
        return False

    def _on_conversations_changed_nomadnet(self, *args):
        # NomadNet calls this for every stored message, but also for every
        # announce and path response of a peer with a chat.
        self._schedule_conversations_changed()

    def _schedule_conversations_changed(self):
        """Tell the UI that the chat list changed, at most once per
        CONVERSATIONS_CHANGED_DELAY (thread-safe): events in that time are
        merged into one reload of the whole list."""
        with self._conv_changed_lock:
            if self._conv_changed_scheduled:
                return
            self._conv_changed_scheduled = True
        GLib.timeout_add(int(CONVERSATIONS_CHANGED_DELAY * 1000), self._flush_conversations_changed)

    def _flush_conversations_changed(self) -> bool:
        with self._conv_changed_lock:
            self._conv_changed_scheduled = False
        self._notify_conversations_changed()
        return False

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

    def _notify_announces(self, batch: List[Dict[str, Any]]) -> bool:
        for cb in self._announce_callbacks:
            try:
                cb(batch)
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
    def get_interface_status(self) -> Dict[str, Dict[str, Any]]:
        """Running interfaces by name: {"type", "online", "rxb", "txb"}.

        Asks RNS, so as client of a shared instance (rnsd, another app)
        these are the shared instance's interfaces, not the local link to it.
        """
        try:
            stats = RNS.Reticulum.get_instance().get_interface_stats() or {}
        except Exception as e:
            RNS.log(f"Retchat: Could not get interface stats: {e}", RNS.LOG_WARNING)
            return {}
        status = {}
        for i in stats.get("interfaces", []):
            name = i.get("short_name") or i.get("name")
            # The shared instance's local sockets (server and its clients)
            if i.get("type") in ("LocalServerInterface", "LocalClientInterface"):
                continue
            if name:
                status[name] = {"type": i.get("type", ""), "online": bool(i.get("status")),
                                "rxb": i.get("rxb", 0), "txb": i.get("txb", 0)}
        return status

    def get_configured_interfaces(self) -> List[Dict[str, Any]]:
        """Interfaces of the Reticulum config, with their live status."""
        cfg_path = self._config_path()

        if not os.path.exists(cfg_path):
            return []

        from RNS.vendor.configobj import ConfigObj
        try:
            cfg = ConfigObj(cfg_path)
            raw_interfaces = cfg.get("interfaces", {})
        except Exception as e:
            RNS.log(f"Retchat: Error reading interfaces from config: {e}", RNS.LOG_ERROR)
            return []

        live = self.get_interface_status()

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

            state = live.get(name, {})

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
                "online": state.get("online", False),
                "rxb": state.get("rxb", 0),
                "txb": state.get("txb", 0),
                "details": " • ".join(details) if details else itype,
                "config": dict(c)
            })

        return results

    def set_interface_enabled(self, name: str, enabled: bool,
                              on_applied: Optional[Callable[[str, str], None]] = None) -> bool:
        """Switch an interface on or off in the Reticulum config and on the
        running instance.

        Returns False if the config couldn't be changed. Applying runs in
        the background; ``on_applied(name, outcome)`` is then called on the
        main loop with outcome "applied", "restart" (saved, but the running
        instance didn't take it, e.g. an older rnsd) or "error".
        """
        cfg_path = self._config_path()

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
        except Exception as e:
            RNS.log(f"Retchat: Failed to toggle interface '{name}': {e}", RNS.LOG_ERROR)
            return False

        self._apply_interface_change(name, "attach" if enabled else "detach", on_applied)
        return True

    def _apply_interface_change(self, name: str, action: str,
                                on_applied: Optional[Callable[[str, str], None]] = None):
        """Attach, detach or reload interface ``name`` on the running instance.

        RNS reads the interface's section from its config file and, as a
        client of a shared instance, asks that instance over RPC. A shared
        instance older than RNS 1.5.6 never answers that request and the
        call has no timeout, so it runs in a thread that is given up on
        after INTERFACE_APPLY_TIMEOUT (the thread stays blocked; it's
        daemonic and holds nothing).
        """
        def call(act):
            box: Dict[str, Any] = {}

            def run():
                try:
                    box["result"] = getattr(RNS.Reticulum.get_instance(), f"{act}_interface")(name)
                except Exception as e:
                    box["error"] = e

            t = threading.Thread(target=run, daemon=True, name=f"Iface-{act}")
            t.start()
            t.join(INTERFACE_APPLY_TIMEOUT)
            if t.is_alive():
                box["timeout"] = True
            return box

        def worker():
            box = call(action)
            if action == "reload" and box.get("result") is None and "error" not in box and "timeout" not in box:
                # Wasn't running: start it
                box = call("attach")
                outcome = self._interface_outcome("attach", name, box)
            else:
                outcome = self._interface_outcome(action, name, box)
            RNS.log(f"Retchat: {action} interface '{name}': {box} -> {outcome}", RNS.LOG_NOTICE)
            if on_applied is not None:
                GLib.idle_add(on_applied, name, outcome)

        threading.Thread(target=worker, daemon=True, name=f"Iface-{name[:16]}").start()

    def _interface_outcome(self, action: str, name: str, box: Dict[str, Any]) -> str:
        """Map RNS's attach/detach/reload result to "applied", "restart" or "error"."""
        if box.get("timeout") or "error" in box:
            # The config is saved; the running instance didn't take it.
            return "restart"
        result = box.get("result")
        if result is True:
            return "applied"
        if result is None:
            # detach: wasn't running (fine); attach: no such config entry
            return "applied" if action == "detach" else "error"
        if action == "attach" and name in self.get_interface_status():
            return "applied"  # was already running
        # Interface management disabled in the shared instance's config,
        # or an interface RNS can't detach (I2P, local)
        return "restart"

    def get_interfaces_info(self) -> List[Dict[str, Any]]:
        """All running interfaces (incl. spawned ones, e.g. AutoInterface peers)."""
        return [{"name": name, **state} for name, state in self.get_interface_status().items()]

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
        if self._page_fetcher is not None:
            self._page_fetcher.cancel()
        try:
            self._save_stamp_costs(force=True)
        except Exception as e:
            RNS.log(f"Retchat: Error saving stamp costs: {e}", RNS.LOG_ERROR)
        try:
            self.app.exit_handler()
        except Exception as e:
            RNS.log(f"Retchat: Error exiting NomadNet: {e}", RNS.LOG_ERROR)
