#!/usr/bin/env python3
"""Show which pins in requirements.txt are behind the latest release on PyPI.

Only reports; updating is deliberate (see "Updating dependencies" in
README.md). Exit status 1 if anything is outdated, 2 on errors.
"""

import json
import os
import sys
import urllib.request

REQUIREMENTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt")


def pins():
    with open(REQUIREMENTS) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                name, version = line.split("==")
                yield name, version


def latest(name):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=20) as response:
        return json.load(response)["info"]["version"]


def main():
    outdated = errors = 0
    for name, pinned in pins():
        try:
            newest = latest(name)
        except Exception as e:
            print(f"{name:20} {pinned:12} ?            ({e})")
            errors += 1
            continue
        mark = "" if newest == pinned else "  <- update available"
        outdated += bool(mark)
        print(f"{name:20} {pinned:12} {newest:12}{mark}")
    if errors:
        return 2
    return 1 if outdated else 0


if __name__ == "__main__":
    sys.exit(main())
