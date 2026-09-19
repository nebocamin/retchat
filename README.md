# Retchat

**Retchat** ist ein moderner Desktop-Chat-Client für das [Reticulum Network](https://reticulum.network/) im **GTK4 + Libadwaita**-Stil (inspiriert von der Android-App [Columba](https://github.com/torlando-tech/columba)).

Mit Retchat kannst du dezentral, serverlos und Ende-zu-Ende-verschlüsselt über das Reticulum-Mesh-Netzwerk per [LXMF](https://github.com/markqvist/LXMF) (Lightweight Extensible Message Format) chatten – über WLAN, TCP-Verbindungen, LoRa-Funk (RNode) und lokale Interfaces.

---

## Highlights & Features

- 🎨 **Modernes GNOME / Libadwaita HIG Design**:
  - Responsives `Adw.NavigationSplitView` (funktioniert auf großen Monitoren genauso wie auf schmalen Fenstern oder Mobilgeräten).
  - Moderne Sprechblasen für gesendete und empfangene Nachrichten mit automatischer Anpassung an Light- und Dark-Themes.
  - Avatare mit Initialen, Zeitstempeln und Hop-Anzeige.
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

## Screenshots & Struktur

```text
retchat/
├── main.py                  # Haupteinstiegspunkt
├── retchat.sh               # Ausführbares Start-Skript
├── retchat.desktop          # Desktop-Starter
├── retchat.svg              # Anwendungs-Icon
├── retchat/
│   ├── app.py               # Adw.Application Lebenszyklus & CSS-Lader
│   ├── window.py            # Hauptfenster (NavigationSplitView, Header, Listen)
│   ├── database.py          # SQLite-Speicher (Unterhaltungen, Nachrichten, Announces)
│   ├── reticulum_service.py # Reticulum & LXMF-Service, Dispatcher & Callbacks
│   ├── style.css            # Libadwaita-CSS (Chat-Bubbles, Badges, Composer)
│   ├── widgets/
│   │   ├── chat_view.py     # Chat-Verlauf & Eingabeleiste
│   │   ├── message_bubble.py# Sprechblasen-Widget mit Status-Symbolen
│   │   ├── conversation_row.py # Zeile in der Unterhaltungsliste
│   │   └── announce_row.py  # Zeile in der Mesh-Entdeckungsliste
│   └── dialogs/
│       ├── new_chat_dialog.py  # Neuer Chat (Hash-Eingabe mit Validierung)
│       ├── profile_dialog.py   # Eigenes Profil & Announce
│       └── interfaces_dialog.py# Reticulum Interface-Status
```

---

## Starten der Anwendung

### Direkt über das Startskript
```bash
./retchat.sh
```

### Über den Desktop-Starter
Retchat ist im Anwendungsmenü unter dem Namen **Retchat** registriert.

---

## Getestet mit
- Kontakt `8d883cfe6c1a846d8f34e5a95a149fdb`
- Reticulum 1.5.4
- LXMF 1.1.1
- GTK 4.0 / Libadwaita 1.9
