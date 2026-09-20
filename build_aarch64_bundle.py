#!/usr/bin/env python3
"""Build a standalone aarch64 Flatpak bundle for Retchat without needing binfmt/qemu."""

import glob
import os
import shutil
import subprocess
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WHEELS_DIR = os.path.join(BASE_DIR, ".wheels-aarch64")
BUILD_DIR = os.path.join(BASE_DIR, ".flatpak-build-aarch64")
REPO_DIR = os.path.join(BASE_DIR, ".flatpak-repo-aarch64")
BUNDLE_PATH = os.path.join(BASE_DIR, "retchat-aarch64.flatpak")


def main():
    print("=== 1. Downloading aarch64 wheels from PyPI ===")
    os.makedirs(WHEELS_DIR, exist_ok=True)
    pkgs = [
        "rns", "lxmf", "msgpack", "cryptography",
        "pyserial", "cffi", "pycparser", "setuptools", "wheel",
        "nomadnet", "qrcode", "urwid", "wcwidth"
    ]
    subprocess.run([
        "pip", "download",
        "--only-binary=:all:",
        "--platform", "manylinux2014_aarch64",
        "--python-version", "3.13",
        "--implementation", "cp",
        "--dest", WHEELS_DIR,
        *pkgs
    ], check=True)

    print("=== 2. Preparing Flatpak app structure ===")
    if os.path.exists(BUILD_DIR):
        shutil.rmtree(BUILD_DIR)
    if os.path.exists(REPO_DIR):
        shutil.rmtree(REPO_DIR)

    # Initialize directory with flatpak build-init
    subprocess.run([
        "flatpak", "build-init",
        BUILD_DIR,
        "org.selfmade.Retchat",
        "org.gnome.Platform",
        "org.gnome.Sdk",
        "50"
    ], check=True)

    files_dir = os.path.join(BUILD_DIR, "files")
    bin_dir = os.path.join(files_dir, "bin")
    site_packages = os.path.join(files_dir, "lib", "python3.13", "site-packages")
    apps_dir = os.path.join(files_dir, "share", "applications")
    icons_dir = os.path.join(files_dir, "share", "icons", "hicolor", "scalable", "apps")
    metainfo_dir = os.path.join(files_dir, "share", "metainfo")

    for d in (bin_dir, site_packages, apps_dir, icons_dir, metainfo_dir):
        os.makedirs(d, exist_ok=True)

    # Extract all aarch64 wheels
    print("=== 3. Unpacking aarch64 dependencies into site-packages ===")
    for whl in glob.glob(os.path.join(WHEELS_DIR, "*.whl")):
        with zipfile.ZipFile(whl) as z:
            z.extractall(site_packages)

    # Copy retchat package
    print("=== 4. Installing retchat package into app directory ===")
    src_retchat = os.path.join(BASE_DIR, "retchat")
    dst_retchat = os.path.join(site_packages, "retchat")
    if os.path.exists(dst_retchat):
        shutil.rmtree(dst_retchat)
    shutil.copytree(src_retchat, dst_retchat)

    # Launcher executable
    launcher_path = os.path.join(bin_dir, "retchat")
    with open(launcher_path, "w") as f:
        f.write("#!/usr/bin/python3\n"
                "import sys\n"
                "from retchat.app import main\n"
                "if __name__ == '__main__':\n"
                "    sys.exit(main())\n")
    os.chmod(launcher_path, 0o755)

    # Desktop entry, icon, and metainfo
    shutil.copy(os.path.join(BASE_DIR, "org.selfmade.Retchat.desktop"), apps_dir)
    shutil.copy(os.path.join(BASE_DIR, "retchat.svg"), os.path.join(icons_dir, "org.selfmade.Retchat.svg"))
    shutil.copy(os.path.join(BASE_DIR, "org.selfmade.Retchat.metainfo.xml"), metainfo_dir)

    # Finalize with flatpak build-finish
    print("=== 5. Finalizing Flatpak metadata ===")
    subprocess.run([
        "flatpak", "build-finish",
        BUILD_DIR,
        "--command=retchat",
        "--share=network",
        "--share=ipc",
        "--socket=fallback-x11",
        "--socket=wayland",
        "--device=all",
        "--filesystem=home",
        "--talk-name=org.freedesktop.Notifications",
        "--runtime=runtime/org.gnome.Platform/aarch64/50",
        "--sdk=runtime/org.gnome.Sdk/aarch64/50"
    ], check=True)

    # Export to OSTree repo
    print("=== 6. Exporting to OSTree repo for aarch64 ===")
    subprocess.run([
        "flatpak", "build-export",
        "--arch=aarch64",
        REPO_DIR,
        BUILD_DIR
    ], check=True)

    # Build single-file bundle
    print("=== 7. Creating standalone bundle: retchat-aarch64.flatpak ===")
    subprocess.run([
        "flatpak", "build-bundle",
        "--arch=aarch64",
        REPO_DIR,
        BUNDLE_PATH,
        "org.selfmade.Retchat"
    ], check=True)

    bundle_size_mb = os.path.getsize(BUNDLE_PATH) / (1024 * 1024)
    print(f"\nSUCCESS! Created {BUNDLE_PATH} ({bundle_size_mb:.1f} MB) for aarch64 (ARM64)!")


if __name__ == "__main__":
    main()
