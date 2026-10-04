<p align="center">
  <img src="data/icons/hicolor/scalable/apps/org.selfmade.Retchat.svg" width="128" height="128" alt="Retchat icon">
</p>

# Retchat

**Retchat** is a desktop and mobile chat client for the [Reticulum Network](https://reticulum.network/), built with **GTK4 and Libadwaita** and inspired by the Android app [Columba](https://github.com/torlando-tech/columba).

Chat without servers and end-to-end encrypted over the Reticulum mesh using [LXMF](https://github.com/markqvist/LXMF), over Wi-Fi, TCP, LoRa radio (RNode) and local interfaces. Retchat runs on the desktop as well as on Linux phones such as the PinePhone or Librem 5 with postmarketOS and Phosh.

The icon is a speech bubble shaped like a ratchet wheel – Retchat, ratchet.

![Direct chat on the desktop](data/screenshots/desktop-chat.png)

<p align="center">
  <img src="data/screenshots/mobile.png" alt="Chat list, conversation and discover tab on a phone">
</p>

![Dark style with the discover tab](data/screenshots/desktop-dark.png)

<sub>Screenshots use dummy data and are generated with `data/screenshots/generate_screenshots.py`.</sub>

---

## Features

- **Direct messages over LXMF**
  - Live delivery status for sent messages: sending, sent to a propagation node (✓), delivered (✓✓), failed.
  - Reply to a message (hover button, right click or long press); the reply shows the quoted message, a click jumps
    to it. Uses the LXMF standard fields (`FIELD_REPLY_TO`, `FIELD_REPLY_QUOTE`), compatible with MeshChatX.
  - React to received messages with emoji (LXMF `FIELD_REACTION`).
  - Rename contacts, copy their address, request a path through the mesh.
  - Start a chat by address, by pasting a contact link (`lxmf://…`, `lxma://…`) or by scanning a QR code with the
    camera (or from an image, e.g. a screenshot). The own address can be shown as QR code. Codes contain address
    and public key (`lxma://`, as in MeshChatX), so a scanned contact can be written to right away, without waiting
    for an announce; the address is checked against the key. Same format as Columba and MeshChatX. Showing the own
    code (and starting a chat from a scanned one) announces right away: delivery also needs a path, and hubs only
    answer path requests for destinations whose announce they have seen.
  - Delete chats from the chat menu, or by right click / long press in the chat list.
- **Images and files**
  - Images are downscaled before sending and shown inline, with a built-in image viewer.
  - Send any other file up to 900 KB (LXMF receivers accept 1000 KB per message by default). Files above 256 KB
    can only be delivered directly, not via propagation nodes – Retchat points this out before sending.
  - Received files show their type, name and size and can be opened or saved. Executables and scripts can only be
    saved, not opened directly.
  - Works with attachments from other LXMF clients (file attachments, images, voice messages).
- **Discover peers on the mesh**
  - The *Entdecken* (discover) tab lists `lxmf.delivery` announces with display name, address and hop count.
  - The list is updated every 5 seconds while it is shown, newest announce on top; while hidden it is not
    touched and catches up when you open it.
  - Start a chat with a single click.
- **Message sync**
  - Fetch messages that were stored for you on an LXMF propagation node while you were offline (main menu → *Nachrichten synchronisieren*).
- **Identity and interfaces**
  - Set your display name, copy your LXMF address and identity hash, announce yourself on the mesh.
  - Overview of all Reticulum interfaces (TCP, AutoInterface, RNode/LoRa) with status and traffic; configure the TCP hub and the propagation node.
  - Interfaces are switched on and off, and a changed TCP hub is reconnected, without a restart; when Retchat uses a shared instance (rnsd, another app), that instance's interfaces are switched (it needs RNS 1.5.6 or newer, otherwise the change takes effect on its next start).
- **Made for phones and desktops**
  - Adaptive layout via `Adw.Breakpoint`: on narrow screens (down to 360 px) sidebar and chat become separate pages with a back button.
  - Native light and dark style, GNOME HIG compliant.
  - The chat always keeps the newest message in view – also when the on-screen keyboard opens or several messages arrive at once.
  - Desktop notifications for incoming messages.

> The user interface is currently in German.

---

## Installation

Prebuilt Flatpak bundles are created with the build script (see below).

### Linux phone (aarch64, e.g. postmarketOS with Phosh)

```bash
scp retchat-aarch64.flatpak user@phone:~/
# on the phone:
flatpak install --user ~/retchat-aarch64.flatpak
```

### Desktop (x86_64)

```bash
flatpak install --user retchat.flatpak
```

Both bundles use the runtime `org.gnome.Platform//50`, which Flatpak installs from Flathub if needed.

To update, install the new bundle into the **same** installation (`--user` or `--system`) as before, otherwise two installations exist side by side and the older one may keep being started.

### Run from source

Requires Python 3, GTK 4.12+, Libadwaita 1.5+ and PyGObject, plus the Python packages pinned in `requirements.txt` (`rns`, `lxmf`, `nomadnet`, …):

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --no-deps -r requirements.txt
.venv/bin/pip install --no-deps -e .
./retchat.sh
```

Tests: `.venv/bin/python -m pytest tests`

---

## Building the Flatpaks

```bash
./build-flatpak.sh            # x86_64 (flatpak-builder)
./build-flatpak.sh --aarch64  # aarch64, without QEMU/binfmt
./build-flatpak.sh --all      # both
```

The aarch64 bundle is cross-packaged by `build_aarch64_bundle.py`: it downloads prebuilt aarch64 wheels from PyPI and assembles the Flatpak without emulation.

Both bundles install exactly the versions in `requirements.txt`, transitive dependencies included. In the x86_64 manifest the file is a source of the `python-dependencies` module, so flatpak-builder rebuilds that module when (and only when) a pin changes; otherwise it reuses its cached build. The versions of a running build are logged at startup and listed in *About → Troubleshooting*.

### Updating dependencies

Retchat hooks into internals of RNS, LXMF and NomadNet (replaced methods, callbacks, tuple layouts). Updates are therefore deliberate, never picked up automatically:

1. `tools/check_updates.py` lists pins with newer releases on PyPI.
2. Raise the pins in `requirements.txt`. For new or changed dependencies of a package, check its metadata (`pip install --dry-run --ignore-installed --report …`); installs use `--no-deps`, so the file must list everything.
3. Update the venv (`.venv/bin/pip install --no-deps -r requirements.txt`) and run the tests. `tests/test_library_contract.py` checks every library internal Retchat relies on; if one fails, adapt the hook before releasing. Also read the release notes for behaviour changes the tests can't see.
4. Rebuild both bundles and update *Tested with* below.

---

## Where data is stored

Retchat uses [NomadNet](https://github.com/markqvist/NomadNet) as its LXMF backend. On the same machine, Retchat and NomadNet therefore **share identity, contacts and messages**.

| Path | Content |
|---|---|
| `~/.nomadnetwork/storage/identity` | Your Reticulum identity (private key) |
| `~/.nomadnetwork/storage/conversations/` | Messages |
| `~/.nomadnetwork/storage/attachments/` | Images and files of received and sent messages |
| `~/.nomadnetwork/config` | NomadNet settings, e.g. announce interval (default: at start and every 6 hours) |
| `~/.reticulum/config` | Reticulum interfaces; on first start Retchat makes sure a TCP interface is configured. Like Reticulum itself, Retchat uses `/etc/reticulum/config` or `~/.config/reticulum/config` instead if one of them exists |
| `~/.local/share/retchat/retchat.db` | Retchat's own data: custom contact names and settings |

---

## Project structure

```text
retchat/
├── org.selfmade.Retchat.json          # Flatpak manifest (x86_64)
├── build-flatpak.sh                   # Flatpak build script
├── build_aarch64_bundle.py            # aarch64 cross-packager
├── org.selfmade.Retchat.metainfo.xml  # AppStream metadata
├── org.selfmade.Retchat.desktop       # Desktop entry (desktop and mobile form factor)
├── setup.py                           # Python package definition
├── requirements.txt                   # Exact dependency versions for all builds
├── tools/check_updates.py             # Lists pins with newer releases on PyPI
├── main.py / retchat.sh               # Entry point / local launcher
├── data/icons/
│   ├── generate_icon.py               # Generates the app icon (ratchet-wheel speech bubble)
│   └── hicolor/                       # Scalable and symbolic app icon
├── data/screenshots/
│   └── generate_screenshots.py        # Renders the README screenshots with dummy data (headless)
└── retchat/
    ├── app.py                         # Adw.Application: lifecycle, CSS, icons, about dialog
    ├── window.py                      # Main window (split view, breakpoints, menus, actions)
    ├── models.py                      # GObject model for messages (MessageItem)
    ├── database.py                    # SQLite store for custom names and settings
    ├── reticulum_service.py           # Reticulum / LXMF / NomadNet service and callbacks
    ├── contact_uri.py                 # Contact links (lxmf://, lxma://): format and verification
    ├── qr.py                          # Creating and reading QR codes (qrcode, zxing-cpp)
    ├── style.css                      # Libadwaita CSS (bubbles, badges, composer)
    ├── widgets/
    │   ├── chat_history.py            # Gtk.ListView history that keeps the newest message in view
    │   ├── chat_view.py               # Direct chat: history and composer
    │   ├── message_bubble.py          # Message bubble with image, files and delivery status
    │   ├── attachment_row.py          # File attachment row (open / save) and file helpers
    │   ├── conversation_row.py        # Row in the chat list
    │   ├── qr_code_view.py            # QR code drawn sharp at any size
    │   ├── qr_scanner.py              # Camera (portal/PipeWire or V4L2) with QR reading
    │   └── announce_row.py            # Row in the discover list
    └── dialogs/
        ├── new_chat_dialog.py         # New chat: address or link, QR scan, own QR code
        ├── profile_dialog.py          # Own profile and announce
        ├── interfaces_dialog.py       # Reticulum interfaces, TCP hub, propagation node
        └── image_viewer_dialog.py     # Image viewer
```

---

## Tested with

- Flatpak runtime `org.gnome.Platform//50` (GTK 4, Libadwaita 1.9)
- Reticulum 1.5.6, LXMF 1.2.0, NomadNet 1.4.4 (pinned in `requirements.txt`)
- Phosh / narrow windows down to 360 px width

---

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
