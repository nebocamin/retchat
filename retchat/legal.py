"""Licenses of the bundled components, for the about dialog.

The full texts are installed to LICENSES_DIR by tools/collect_licenses.py
(Flatpak builds). Keep this list in sync with requirements.txt
(tests/test_licenses.py checks it).
"""

import os
from importlib import metadata
from typing import List, Optional, Tuple

LICENSES_DIR = "/app/share/licenses/org.selfmade.Retchat"
_SOURCE_LICENSES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "licenses")

# (title, package, copyright) of the Reticulum stack, shown with full license text
RETICULUM = [
    ("Reticulum (RNS)", "rns", "© 2016–2026 Mark Qvist"),
    ("LXMF", "lxmf", "© 2020–2025 Mark Qvist"),
]
NOMADNET = ("Nomad Network", "nomadnet", "© Mark Qvist")
# (title, package, license) of the other bundled libraries
LIBRARIES = [
    ("cryptography", "cryptography", "Apache-2.0 oder BSD-3-Clause"),
    ("msgpack", "msgpack", "Apache-2.0"),
    ("Pillow", "pillow", "MIT-CMU"),
    ("qrcode", "qrcode", "BSD-3-Clause"),
    ("zxing-cpp", "zxing-cpp", "Apache-2.0"),
    ("pySerial", "pyserial", "BSD-3-Clause"),
    ("urwid", "urwid", "LGPL-2.1"),
    ("cffi", "cffi", "MIT-0"),
    ("pycparser", "pycparser", "BSD-3-Clause"),
    ("wcwidth", "wcwidth", "MIT"),
    ("typing_extensions", "typing-extensions", "PSF-2.0"),
]


def bundled_packages() -> List[str]:
    return [p for _t, p, _c in RETICULUM] + [NOMADNET[1]] + [p for _t, p, _l in LIBRARIES]


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return ""


def license_text(package: str) -> Optional[str]:
    """Full license text of a bundled package, if installed with Retchat."""
    for path in (os.path.join(LICENSES_DIR, package, "LICENSE"),
                 os.path.join(_SOURCE_LICENSES, package + ".txt")):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def legal_sections() -> List[Tuple[str, Optional[str], str, Optional[str]]]:
    """(title, copyright, kind, text) per section; kind "gpl3" or "custom"."""
    sections = []
    for title, package, copyright in RETICULUM:
        text = license_text(package) or (
            "Reticulum License: MIT-artig, mit zwei Nutzungsbeschränkungen (keine Systeme, die Menschen "
            "absichtlich schaden können; keine Erstellung von KI-Trainingsdaten).")
        sections.append((f"{title} {_version(package)}".strip(), copyright, "custom", text))

    title, package, copyright = NOMADNET
    sections.append((f"{title} {_version(package)}".strip(),
                     f"{copyright}\nRetchats Micron-Parser ist aus Nomad Network portiert und angepasst.",
                     "gpl3", None))

    lines = [f"{title} {_version(package)} – {lic}".replace("  ", " ") for title, package, lic in LIBRARIES]
    lines.append(f"\nDie vollständigen Lizenztexte liegen in {LICENSES_DIR}.")
    sections.append(("Weitere Bibliotheken", None, "custom", "\n".join(lines)))
    return sections
