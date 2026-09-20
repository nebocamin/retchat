#!/usr/bin/env bash
# Build script to create standalone Flatpak bundles (.flatpak) for Retchat
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

build_aarch64() {
    echo "=== Erstelle Flatpak für aarch64 (ARM64 postmarketOS) ==="
    python3 "$SCRIPT_DIR/build_aarch64_bundle.py"
    echo ""
    echo "Zur Installation auf dem postmarketOS-Telefon (aarch64):"
    echo "1. Datei 'retchat-aarch64.flatpak' auf das Telefon kopieren:"
    echo "   scp retchat-aarch64.flatpak user@telefon:~/"
    echo "2. Auf dem Telefon ausführen:"
    echo "   flatpak install --user ~/retchat-aarch64.flatpak"
}

build_x86_64() {
    echo "=== Erstelle Flatpak für x86_64 (Desktop) ==="
    BUILD_DIR="$SCRIPT_DIR/.flatpak-build"
    REPO_DIR="$SCRIPT_DIR/.flatpak-repo"
    BUNDLE_FILE="$SCRIPT_DIR/retchat.flatpak"

    flatpak-builder --force-clean --disable-rofiles-fuse --repo="$REPO_DIR" "$BUILD_DIR" org.selfmade.Retchat.json
    flatpak build-bundle "$REPO_DIR" "$BUNDLE_FILE" org.selfmade.Retchat
    echo "Flatpak-Bundle für x86_64 erstellt: $BUNDLE_FILE"
}

case "$1" in
    --aarch64|aarch64|arm64)
        build_aarch64
        ;;
    --all|all)
        build_x86_64
        build_aarch64
        ;;
    *)
        build_x86_64
        ;;
esac
