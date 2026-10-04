"""Contact links (QR codes) for LXMF addresses.

Formats, as used by MeshChatX and others:

- ``lxma://<destination hash>:<public key>``: the LXMF delivery address and
  the identity's public key (64 bytes, as hex). Whoever scans it knows the
  identity right away and can send encrypted messages without waiting for
  an announce. The address is checked against the key.
- ``lxmf://<destination hash>`` (also ``lxmf:`` / ``lxmf@``): the address
  only.
- A bare 32 character hex address.
"""

import re
from dataclasses import dataclass
from typing import Optional

import RNS

LXMA_SCHEME = "lxma://"
_HEX_DEST = r"[0-9a-f]{32}"
_LXMA_RE = re.compile(rf"lxma://({_HEX_DEST}):([0-9a-f]+)")
_LXMF_RE = re.compile(rf"(?:lxmf://|lxmf:|lxmf@)?({_HEX_DEST})/?")
# Identity public key: X25519 + Ed25519, 32 bytes each
PUBLIC_KEY_HEX_LEN = RNS.Identity.KEYSIZE // 8 * 2


@dataclass(frozen=True)
class Contact:
    destination_hash: str  # lowercase hex
    public_key: Optional[bytes] = None  # verified against the address


def contact_uri(destination_hash: str, public_key: Optional[bytes]) -> str:
    """The link shown as QR code for an LXMF address."""
    if public_key:
        return f"{LXMA_SCHEME}{destination_hash.lower()}:{public_key.hex()}"
    return f"lxmf://{destination_hash.lower()}"


def delivery_hash_for_key(public_key: bytes) -> str:
    identity = RNS.Identity(create_keys=False)
    identity.load_public_key(public_key)
    return RNS.Destination.hash(identity, "lxmf", "delivery").hex()


def parse_contact(text: str) -> Contact:
    """Parse an address or contact link; raises ValueError with a message for the user."""
    value = "".join((text or "").split()).lower()
    if not value:
        raise ValueError("Keine Adresse")

    m = _LXMA_RE.fullmatch(value)
    if m:
        dest, key_hex = m.groups()
        if len(key_hex) != PUBLIC_KEY_HEX_LEN:
            # MeshChatX also accepts a 32 byte key; that isn't a full
            # identity, so only the address is used.
            if len(key_hex) == PUBLIC_KEY_HEX_LEN // 2:
                return Contact(dest)
            raise ValueError("Ungültiger Schlüssel im Kontakt-Link")
        public_key = bytes.fromhex(key_hex)
        try:
            matches = delivery_hash_for_key(public_key) == dest
        except Exception:
            matches = False
        if not matches:
            raise ValueError("Adresse und Schlüssel im Kontakt-Link passen nicht zusammen")
        return Contact(dest, public_key)

    m = _LXMF_RE.fullmatch(value)
    if m:
        return Contact(m.group(1))

    if value.startswith("lxm://"):
        raise ValueError("Das ist eine Papier-Nachricht, keine Kontaktadresse")
    if value.startswith("nomadnetwork://") or value.startswith(":/page"):
        raise ValueError("Das ist ein Link auf eine NomadNet-Seite, keine Kontaktadresse")
    if re.fullmatch(r"[0-9a-f]+", value):
        raise ValueError(f"Eine Adresse hat 32 Hex-Zeichen, das sind {len(value)}")
    raise ValueError("Keine LXMF-Adresse und kein Kontakt-Link")
