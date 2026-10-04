#!/usr/bin/env python3
"""Collect the license texts of everything bundled into a Flatpak.

Usage: collect_licenses.py [--app-license LICENSE] <site-packages> <target dir>

Copies Retchat's own license, and for every installed Python package its
license files from the .dist-info directory (LICENSE*, COPYING*, NOTICE*,
licenses/...) into <target>/<package>/. Packages whose wheels ship no
license file (rns, lxmf, pyserial) get the text from data/licenses/. Writes
<target>/README with package, version and license, and fails if a package
ends up without a license text: most licenses require the text to be
included with every copy.
"""

import argparse
import os
import re
import shutil
import sys
from email.parser import Parser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FALLBACK_DIR = os.path.join(ROOT, "data", "licenses")
LICENSE_FILE_RE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.I)
SKIP = {"retchat"}  # covered by --app-license
# Where the metadata is wrong or vague (checked against the license texts)
LICENSE_OVERRIDES = {
    "nomadnet": "GPL-3.0 (its metadata says MIT; the license text is the GPL)",
    "rns": "Reticulum License (MIT-style, with restrictions on use)",
    "lxmf": "Reticulum License (MIT-style, with restrictions on use)",
}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def license_name(meta) -> str:
    """License as declared in the package metadata (for the README)."""
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    declared = (meta.get("License") or "").strip()
    if declared and len(declared) < 60 and "\n" not in declared:
        return declared
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    return ", ".join(classifiers) or "see files"


def collect(site_packages: str, target: str, app_license: str = None) -> list:
    """Copy the license texts; returns the packages without one."""
    os.makedirs(target, exist_ok=True)
    if app_license:
        shutil.copy(app_license, os.path.join(target, "LICENSE"))

    rows, missing = [], []
    for entry in sorted(os.listdir(site_packages)):
        if not entry.endswith(".dist-info"):
            continue
        dist = os.path.join(site_packages, entry)
        with open(os.path.join(dist, "METADATA"), encoding="utf-8") as f:
            meta = Parser().parse(f, headersonly=True)
        name = normalize(meta["Name"])
        if name in SKIP:
            continue
        dest = os.path.join(target, name)
        copied = []
        for base, _dirs, files in os.walk(dist):
            for file in files:
                rel = os.path.relpath(os.path.join(base, file), dist)
                if LICENSE_FILE_RE.search(rel):
                    out = os.path.join(dest, rel.replace("licenses" + os.sep, "", 1))
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    shutil.copy(os.path.join(base, file), out)
                    copied.append(rel)
        fallback = os.path.join(FALLBACK_DIR, name + ".txt")
        if not copied and os.path.isfile(fallback):
            os.makedirs(dest, exist_ok=True)
            shutil.copy(fallback, os.path.join(dest, "LICENSE"))
            copied.append("data/licenses/" + name + ".txt")
        if not copied:
            missing.append(name)
        rows.append((name, meta["Version"], LICENSE_OVERRIDES.get(name) or license_name(meta)))

    with open(os.path.join(target, "README"), "w", encoding="utf-8") as f:
        f.write("Retchat is licensed under the GNU GPL, version 3 or later (LICENSE).\n"
                "It bundles these Python packages; their license texts are in the\n"
                "directories of the same name.\n\n")
        width = max(len(r[0]) for r in rows) if rows else 10
        for name, version, lic in rows:
            f.write(f"{name:<{width}}  {version:<10}  {lic}\n")
    return missing


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-license")
    parser.add_argument("site_packages")
    parser.add_argument("target")
    args = parser.parse_args()
    missing = collect(args.site_packages, args.target, args.app_license)
    if missing:
        print(f"No license text for: {', '.join(missing)} (add data/licenses/<name>.txt)", file=sys.stderr)
        return 1
    print(f"License texts collected in {args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
