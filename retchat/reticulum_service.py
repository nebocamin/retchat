"""Reticulum and LXMF Service integration for Retchat."""

import getpass
import os
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import gi
gi.require_version('GLib', '2.0')
from gi.repository import GLib

import LXMF
import msgpack
import RNS

from retchat.database import Database

# Message states
STATE_SENDING = 0
STATE_SENT = 1
STATE_DELIVERED = 2
STATE_FAILED = 3


class ReticulumService:
    aspect_filter = None  # Catch announces for discovery

    def __init__(self, db: Database):
        self.db = db
        self.config_dir = os.path.expanduser("~/.config/retchat")
        self.data_dir = os.path.expanduser("~/.local/share/retchat")
        self.storage_path = os.path.join(self.data_dir, "lxmf")
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.storage_path, exist_ok=True)

        self.identity_path = os.path.join(self.config_dir, "identity")
        self.identity = self._load_or_create_identity()

        # Display name
        saved_name = self.db.get_setting("display_name")
        if not saved_name:
            try:
                saved_name = getpass.getuser().capitalize()
            except Exception:
                saved_name = "Retchat"
            self.db.set_setting("display_name", saved_name)
        self.display_name = saved_name

        # Callbacks for UI updates
        self._message_received_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._message_state_callbacks: List[Callable[[str, int], None]] = []
        self._announce_callbacks: List[Callable[[Dict[str, Any]], None]] = []

        # Ensure ~/.reticulum/config has TCP Client Interface enabled as default (crucial for mobile / Phosh)
        self._ensure_tcp_default_config()

        # Reticulum and LXMRouter initialization
        self.rns = RNS.Reticulum()
        self.router = LXMF.LXMRouter(identity=self.identity, storagepath=self.storage_path)
        self.delivery_dest = self.router.register_delivery_identity(
            self.identity, display_name=self.display_name
        )
        self.router.register_delivery_callback(self._on_inbound_lxmessage)

        # Register announce handler
        RNS.Transport.register_announce_handler(self)

    def _ensure_tcp_default_config(self):
        """Ensure ~/.reticulum/config exists with TCP enabled as default for mobile/cellular networks."""
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
                    "enable_transport": "True",
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
                RNS.log("Retchat: Created mobile-friendly default Reticulum config with TCP enabled", RNS.LOG_NOTICE)
            else:
                cfg = ConfigObj(cfg_path)
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
                    cfg.write()
                    RNS.log("Retchat: Added default TCP Client interface to existing config", RNS.LOG_NOTICE)
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
                "enable_transport": "True",
                "share_instance": "Yes",
                "instance_name": "default"
            }
        if "interfaces" not in cfg:
            cfg["interfaces"] = {}

        # Update or add TCP interface
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

        # Update AutoInterface
        if "Default Interface" in cfg["interfaces"]:
            cfg["interfaces"]["Default Interface"]["enabled"] = "no" if disable_auto else "yes"
        elif disable_auto:
            cfg["interfaces"]["Default Interface"] = {
                "type": "AutoInterface",
                "enabled": "no"
            }

        cfg.write()
        return cfg_path

        RNS.log(
            f"Retchat: ReticulumService initialised. Identity: {self.identity.hash.hex()}, "
            f"Delivery destination: {self.delivery_destination_hex}",
            RNS.LOG_NOTICE
        )

    def _load_or_create_identity(self) -> RNS.Identity:
        if os.path.exists(self.identity_path):
            try:
                identity = RNS.Identity.from_file(self.identity_path)
                if identity:
                    return identity
            except Exception as e:
                RNS.log(f"Retchat: Failed to load identity from {self.identity_path}: {e}", RNS.LOG_ERROR)

        # Create new identity
        identity = RNS.Identity()
        identity.to_file(self.identity_path)
        return identity

    @property
    def identity_hex(self) -> str:
        return self.identity.hash.hex()

    @property
    def delivery_destination_hex(self) -> str:
        return self.delivery_dest.hash.hex()

    def add_message_received_callback(self, cb: Callable[[Dict[str, Any]], None]):
        self._message_received_callbacks.append(cb)

    def add_message_state_callback(self, cb: Callable[[str, int], None]):
        self._message_state_callbacks.append(cb)

    def add_announce_callback(self, cb: Callable[[Dict[str, Any]], None]):
        self._announce_callbacks.append(cb)

    # --- Announce Handling ---
    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: Optional[RNS.Identity],
        app_data: Optional[bytes],
        announce_packet_hash: Optional[bytes] = None,
        is_path_response: bool = False
    ):
        """Called by RNS Transport when an announce is received."""
        try:
            dest_hex = destination_hash.hex()
            id_hex = announced_identity.hash.hex() if announced_identity else None

            # Check aspect
            aspect = None
            if announced_identity:
                for a in ("lxmf.delivery", "lxmf.propagation", "nomadnetwork.node"):
                    try:
                        if RNS.Destination.hash_from_name_and_identity(a, announced_identity) == destination_hash:
                            aspect = a
                            break
                    except Exception:
                        pass

            # Only track lxmf.delivery announces for chat peers
            if aspect != "lxmf.delivery" and aspect is not None:
                return

            display_name = None
            if app_data:
                try:
                    unpacked = msgpack.unpackb(app_data)
                    if isinstance(unpacked, list) and len(unpacked) > 0 and unpacked[0]:
                        display_name = unpacked[0].decode("utf-8", errors="replace")
                except Exception:
                    try:
                        display_name = app_data.decode("utf-8", errors="replace")
                    except Exception:
                        pass

            hops = RNS.Transport.hops_to(destination_hash)
            if hops == RNS.Transport.PATHFINDER_M:
                hops = 0

            # Determine receiving interface
            iface_str = None
            try:
                path_entry = RNS.Transport.path_table.get(destination_hash)
                if path_entry and len(path_entry) > 5 and path_entry[5]:
                    iface_str = str(path_entry[5])
            except Exception:
                pass

            now = time.time()
            self.db.save_announce(
                dest_hash=dest_hex,
                identity_hash=id_hex,
                display_name=display_name,
                hops=hops,
                aspect=aspect or "lxmf.delivery",
                receiving_interface=iface_str,
                last_seen=now
            )

            announce_data = {
                "destination_hash": dest_hex,
                "identity_hash": id_hex,
                "display_name": display_name,
                "hops": hops,
                "aspect": aspect or "lxmf.delivery",
                "receiving_interface": iface_str,
                "last_seen": now
            }

            # Dispatch to UI thread
            GLib.idle_add(self._notify_announce, announce_data)

        except Exception as e:
            RNS.log(f"Retchat: Error handling announce: {e}", RNS.LOG_ERROR)

    def _notify_announce(self, data: Dict[str, Any]) -> bool:
        for cb in self._announce_callbacks:
            try:
                cb(data)
            except Exception as e:
                RNS.log(f"Retchat: Error in announce callback: {e}", RNS.LOG_ERROR)
        return False

    # --- Inbound Message Handling ---
    def _on_inbound_lxmessage(self, message: LXMF.LXMessage):
        """Called by LXMRouter when a new inbound message is received."""
        try:
            msg_hash = message.hash.hex()
            sender_hex = message.source_hash.hex()
            recipient_hex = message.destination_hash.hex()
            content = message.content_as_string()
            msg_time = message.timestamp if message.timestamp else time.time()

            hops = RNS.Transport.hops_to(message.source_hash)
            if hops == RNS.Transport.PATHFINDER_M:
                hops = 0

            # Store in DB
            # 1. Update/create conversation
            self.db.add_or_update_conversation(
                dest_hash=sender_hex,
                hops=hops
            )
            self.db.update_conversation_last_message(
                dest_hash=sender_hex,
                last_text=content,
                last_time=msg_time,
                increment_unread=True
            )

            # 2. Store message
            self.db.add_message(
                message_hash=msg_hash,
                conversation_hash=sender_hex,
                sender_hash=sender_hex,
                recipient_hash=recipient_hex,
                is_outgoing=False,
                content=content,
                timestamp=msg_time,
                state=STATE_DELIVERED,
                hops=hops
            )

            # Send desktop notification
            self._trigger_notification(sender_hex, content)

            msg_dict = {
                "message_hash": msg_hash,
                "conversation_hash": sender_hex,
                "sender_hash": sender_hex,
                "recipient_hash": recipient_hex,
                "is_outgoing": False,
                "content": content,
                "timestamp": msg_time,
                "state": STATE_DELIVERED,
                "hops": hops
            }

            GLib.idle_add(self._notify_message_received, msg_dict)

        except Exception as e:
            RNS.log(f"Retchat: Error handling inbound message: {e}", RNS.LOG_ERROR)

    def _trigger_notification(self, sender_hex: str, content: str):
        try:
            conv = self.db.get_conversation(sender_hex)
            sender_name = conv.get("custom_name") or conv.get("display_name") or sender_hex[:8]
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

    def _notify_message_received(self, msg_dict: Dict[str, Any]) -> bool:
        for cb in self._message_received_callbacks:
            try:
                cb(msg_dict)
            except Exception as e:
                RNS.log(f"Retchat: Error in message received callback: {e}", RNS.LOG_ERROR)
        return False

    # --- Outbound Message Sending ---
    def send_message(
        self,
        dest_hex: str,
        content: str,
        desired_method: int = LXMF.LXMessage.OPPORTUNISTIC
    ) -> Dict[str, Any]:
        """Send an LXMF message to a destination hash."""
        dest_hex = dest_hex.strip().lower()
        if len(dest_hex) != 32:
            raise ValueError("Ungültige Zieladresse: Muss ein 32-Zeichen Hex-Hash sein.")

        dest_bytes = bytes.fromhex(dest_hex)

        # Check if recipient Identity is recalled
        recipient_id = RNS.Identity.recall(dest_bytes)
        if recipient_id is None:
            # Try to request path and check again
            RNS.Transport.request_path(dest_bytes)
            # Give a brief moment for path resolution if available
            deadline = time.time() + 1.5
            while recipient_id is None and time.time() < deadline:
                time.sleep(0.1)
                recipient_id = RNS.Identity.recall(dest_bytes)

        if recipient_id is None:
            raise RuntimeError(
                f"Die Identität für Ziel <{dest_hex}> ist noch nicht bekannt. "
                "Eine Pfadanfrage wurde im Mesh gesendet. Bitte versuche es in wenigen Sekunden erneut."
            )

        recipient_dest = RNS.Destination(
            recipient_id,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery"
        )

        lxm = LXMF.LXMessage(
            destination=recipient_dest,
            source=self.delivery_dest,
            content=content,
            title="",
            desired_method=desired_method
        )

        lxm.register_delivery_callback(self._on_outbound_delivered)
        lxm.register_failed_callback(self._on_outbound_failed)

        self.router.handle_outbound(lxm)

        msg_hash = lxm.hash.hex() if lxm.hash else f"temp_{int(time.time()*1000)}"
        now = time.time()

        hops = RNS.Transport.hops_to(dest_bytes)
        if hops == RNS.Transport.PATHFINDER_M:
            hops = 0

        # Save to DB
        self.db.add_or_update_conversation(dest_hash=dest_hex, hops=hops)
        self.db.update_conversation_last_message(
            dest_hash=dest_hex,
            last_text=content,
            last_time=now,
            increment_unread=False
        )
        self.db.add_message(
            message_hash=msg_hash,
            conversation_hash=dest_hex,
            sender_hash=self.delivery_destination_hex,
            recipient_hash=dest_hex,
            is_outgoing=True,
            content=content,
            timestamp=now,
            state=STATE_SENDING,
            hops=hops
        )

        return {
            "message_hash": msg_hash,
            "conversation_hash": dest_hex,
            "sender_hash": self.delivery_destination_hex,
            "recipient_hash": dest_hex,
            "is_outgoing": True,
            "content": content,
            "timestamp": now,
            "state": STATE_SENDING,
            "hops": hops
        }

    def _on_outbound_delivered(self, message: LXMF.LXMessage):
        """Called when delivery acknowledgment is received."""
        msg_hash = message.hash.hex()
        RNS.log(f"Retchat: Outbound message {msg_hash} delivered!", RNS.LOG_INFO)
        self.db.update_message_state(msg_hash, STATE_DELIVERED)
        GLib.idle_add(self._notify_message_state, msg_hash, STATE_DELIVERED)

    def _on_outbound_failed(self, message: LXMF.LXMessage):
        """Called when message transmission fails."""
        msg_hash = message.hash.hex()
        RNS.log(f"Retchat: Outbound message {msg_hash} failed!", RNS.LOG_WARNING)
        self.db.update_message_state(msg_hash, STATE_FAILED)
        GLib.idle_add(self._notify_message_state, msg_hash, STATE_FAILED)

    def _notify_message_state(self, message_hash: str, state: int) -> bool:
        for cb in self._message_state_callbacks:
            try:
                cb(message_hash, state)
            except Exception as e:
                RNS.log(f"Retchat: Error in message state callback: {e}", RNS.LOG_ERROR)
        return False

    # --- Profile & Announce Management ---
    def set_display_name(self, new_name: str, announce_now: bool = True):
        new_name = new_name.strip()
        if not new_name:
            return
        self.display_name = new_name
        self.db.set_setting("display_name", new_name)
        self.delivery_dest.display_name = new_name
        if announce_now:
            self.announce()

    def announce(self):
        """Broadcast an announce packet on all active interfaces."""
        try:
            self.router.announce(self.delivery_dest.hash)
            RNS.log(f"Retchat: Announced delivery destination {self.delivery_destination_hex}", RNS.LOG_INFO)
        except Exception as e:
            RNS.log(f"Retchat: Failed to announce: {e}", RNS.LOG_ERROR)

    # --- Interfaces Status ---
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

    def shutdown(self):
        """Clean shutdown of LXMRouter and Reticulum."""
        try:
            self.router.exit_handler()
        except Exception as e:
            RNS.log(f"Retchat: Error exiting LXMRouter: {e}", RNS.LOG_ERROR)
