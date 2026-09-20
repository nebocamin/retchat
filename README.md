# Retchat

**Retchat** ist ein moderner Desktop- und Mobile-Chat-Client für das [Reticulum Network](https://reticulum.network/) im **GTK4 + Libadwaita**-Stil (inspiriert von der Android-App [Columba](https://github.com/torlando-tech/columba)).

Mit Retchat kannst du dezentral, serverlos und Ende-zu-Ende-verschlüsselt über das Reticulum-Mesh-Netzwerk per [LXMF](https://github.com/markqvist/LXMF) (Lightweight Extensible Message Format) chatten – über WLAN, TCP-Verbindungen, LoRa-Funk (RNode) und lokale Interfaces.

---

## Highlights & Features

- 📱 **Phosh & PostmarketOS Mobile-optimiert**:
  - Dank `Adw.Breakpoint` passt sich die App automatisch an schmale Bildschirme (360x720px PinePhone, Librem 5 usw.) an.
  - Das Fenster lässt sich flexibel bis auf 300px Breite verkleinern.
  - Auf Telefonen klappt die Ansicht in eine intuitive Einzelansicht (Seitenleiste ↔ Chat) mit automatischem Zurück-Button um.
- 🎨 **Modernes GNOME / Libadwaita HIG Design**:
  - Responsives `Adw.NavigationSplitView`.
  - Sprechblasen mit nativer Light- und Dark-Theme-Anpassung.
  - Avatare mit Initialen, Zeitstempeln und Hop-Zählern.
- 📬 **LXMF-Nachrichtenversand & Empfangsbestätigungen**:
  - Live-Zustellungsstatus (`🕒 Wird gesendet` ➔ `✓ Gesendet` ➔ `✓✓ Zugestellt` ➔ `❌ Fehlgeschlagen`).
  - Schneller Nachrichtenversand per Enter-Taste.
- 📡 **Mesh-Entdeckung & Announce-Listener**:
  - Eigener Tab **"Entdecken"**: Hört kontinuierlich auf `lxmf.delivery`-Ankündigungen im Reticulum-Netzwerk.
  - Gefundene Peers werden mit Anzeigename, Zieladresse, Empfangs-Interface und Hop-Count aufgelistet.
  - Mit nur einem Klick auf den Chat-Button kann direkt eine Unterhaltung gestartet werden.
- 👤 **Identitäts- & Profil-Verwaltung**:
  - Eigener Anzeigename (wird bei Ankündigungen im Mesh mitgeteilt).
  - Schnelles Kopieren der eigenen **LXMF-Zieladresse** (Destination Hash) und des **Identitäts-Hashes**.
  - Manueller "Jetzt im Mesh ankündigen (Announce)"-Button.
  - Sichere Schlüsselspeicherung in `~/.config/retchat/identity`.
- 🔌 **Schnittstellen-Status**:
  - Dialog zur Echtzeit-Übersicht aller aktiven Reticulum-Interfaces (TCP-Clients, AutoInterface, RNode-LoRa) samt Online-Status und Traffic-Statistiken (RX/TX Bytes).
- 💾 **Robuste lokale Persistenz**:
  - Lokale SQLite-Datenbank (`~/.local/share/retchat/retchat.db`) für Nachrichten, Kontakte und Mesh-Peers.
  - Desktop-Benachrichtigungen bei eingehenden Nachrichten (`notify-send`).

---

## Installation & Ausführung

### 1. Als Flatpak (Empfohlen für postmarketOS / Phosh)

Das fertige Flatpak-Bundle `retchat.flatpak` befindet sich im Projektverzeichnis und kann direkt auf jedem Linux- oder postmarketOS-Gerät installiert werden:

```bash
flatpak install --user retchat.flatpak
```

Zum erneuten Erstellen des Flatpaks:
```bash
./build-flatpak.sh
```

### 2. Direkt über das Startskript (Desktop / Entwicklung)
```bash
./retchat.sh
```

---

## Projektstruktur

```text
retchat/
├── org.selfmade.Retchat.json     # Flatpak-Manifest
├── build-flatpak.sh              # Automatisches Flatpak-Build-Skript
├── retchat.flatpak               # Fertiges Flatpak-Bundle (5.1 MB)
├── org.selfmade.Retchat.metainfo.xml # AppStream Metadaten
├── org.selfmade.Retchat.desktop  # Desktop-Starter mit FormFactor-Unterstützung
├── setup.py                      # Python Package Definition
├── main.py                       # Haupteinstiegspunkt
├── retchat.sh                    # Lokales Ausführskript
├── retchat.svg                   # Anwendungs-Icon
├── retchat/
│   ├── app.py                    # Adw.Application Lebenszyklus & CSS-Lader
│   ├── window.py                 # Hauptfenster (NavigationSplitView, Breakpoints)
│   ├── database.py               # SQLite-Speicher (Unterhaltungen, Nachrichten, Announces)
│   ├── reticulum_service.py      # Reticulum & LXMF-Service, Dispatcher & Callbacks
│   ├── style.css                 # Libadwaita-CSS (Chat-Bubbles, Badges, Composer)
│   ├── widgets/
│   │   ├── chat_view.py          # Chat-Verlauf & Eingabeleiste
│   │   ├── message_bubble.py     # Sprechblasen-Widget mit Status-Symbolen
│   │   ├── conversation_row.py   # Zeile in der Unterhaltungsliste
│   │   └── announce_row.py       # Zeile in der Mesh-Entdeckungsliste
│   └── dialogs/
│       ├── new_chat_dialog.py    # Neuer Chat (Hash-Eingabe mit Validierung)
│       ├── profile_dialog.py     # Eigenes Profil & Announce
│       └── interfaces_dialog.py  # Reticulum Interface-Status
```

---

## Getestet mit
- Phosh Mobile Breakpoint (bis 300px Fensterbreite verkleinerbar)
- Flatpak Runtime `org.gnome.Platform//50`
- Kontakt `8d883cfe6c1a846d8f34e5a95a149fdb`
- Reticulum 1.5.4
- LXMF 1.1.1
- GTK 4.0 / Libadwaita 1.9
