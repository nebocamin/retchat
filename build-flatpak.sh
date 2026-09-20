#!/usr/bin/env bash
# Build script to create a standalone Flatpak bundle (.flatpak) for Retchat
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_DIR="$SCRIPT_DIR/.flatpak-build"
REPO_DIR="$SCRIPT_DIR/.flatpak-repo"
BUNDLE_FILE="$SCRIPT_DIR/retchat.flatpak"

echo "=== 1. Building Flatpak with flatpak-builder ==="
flatpak-builder --force-clean --disable-rofiles-fuse --repo="$REPO_DIR" "$BUILD_DIR" org.selfmade.Retchat.json

echo "=== 2. Creating single-file bundle: retchat.flatpak ==="
flatpak build-bundle "$REPO_DIR" "$BUNDLE_FILE" org.selfmade.Retchat

echo "=== Fertig! ==="
echo "Flatpak-Bundle erfolgreich erstellt: $BUNDLE_FILE"
echo ""
echo "Zur Installation auf dem postmarketOS-Telefon:"
echo "1. Datei 'retchat.flatpak' auf das Telefon übertragen (z.B. per scp oder USB)"
echo "2. Auf dem Telefon ausführen: flatpak install --user retchat.flatpak"
