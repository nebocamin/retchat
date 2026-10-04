"""Every bundled package has its license text, and the about dialog lists them all."""

import os
import re
import sys
from importlib import metadata

import pytest

from retchat import legal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import collect_licenses  # noqa: E402


def _normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins():
    with open(os.path.join(ROOT, "requirements.txt")) as f:
        return [_normalize(line.split("#")[0].strip().split("==")[0]) for line in f
                if line.split("#")[0].strip()]


def test_about_dialog_lists_every_bundled_package():
    """A new dependency needs an entry in retchat/legal.py."""
    assert sorted(_normalize(p) for p in legal.bundled_packages()) == sorted(_pins())


@pytest.mark.parametrize("package", _pins())
def test_license_text_available(package):
    """From the package itself, or data/licenses/<name>.txt for wheels without one.

    Packages from the system (a venv with system site-packages) are skipped:
    distributions move license texts elsewhere. The Flatpak builds install
    wheels; tools/collect_licenses.py fails the build if a text is missing.
    """
    dist = metadata.distribution(package)
    if not str(dist.locate_file("")).startswith(sys.prefix):
        pytest.skip(f"{package} is a system package")
    files = dist.files or []
    shipped = [f for f in files if ".dist-info" in str(f) and collect_licenses.LICENSE_FILE_RE.search(str(f))]
    fallback = os.path.join(ROOT, "data", "licenses", package + ".txt")
    assert shipped or os.path.isfile(fallback), f"no license text for {package}: add data/licenses/{package}.txt"


def test_reticulum_license_texts():
    for package in ("rns", "lxmf"):
        text = legal.license_text(package)
        assert text.startswith("Reticulum License") and "Mark Qvist" in text
        assert "shall not be used" in text  # the use restrictions are part of it


def _dist(site, name, version, license_files=(), license_field="MIT"):
    d = site / f"{name}-{version}.dist-info"
    d.mkdir(parents=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nLicense: {license_field}\n")
    for rel in license_files:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(f"license of {name}")


def test_collector(tmp_path):
    site = tmp_path / "site"
    _dist(site, "withfile", "1.0", ["licenses/LICENSE"])
    _dist(site, "rns", "1.5.6", license_field="Reticulum License")  # no file: data/licenses/rns.txt
    _dist(site, "nothing", "2.0")
    app_license = tmp_path / "APP"
    app_license.write_text("GPL")
    target = tmp_path / "out"

    missing = collect_licenses.collect(str(site), str(target), str(app_license))

    assert missing == ["nothing"]
    assert (target / "LICENSE").read_text() == "GPL"
    assert (target / "withfile" / "LICENSE").read_text() == "license of withfile"
    assert (target / "rns" / "LICENSE").read_text().startswith("Reticulum License")
    readme = (target / "README").read_text()
    assert "rns" in readme and "with restrictions on use" in readme


def test_legal_sections():
    sections = legal.legal_sections()
    titles = [s[0] for s in sections]
    assert titles[0].startswith("Reticulum (RNS)") and titles[1].startswith("LXMF")
    assert sections[2][2] == "gpl3" and "Micron" in sections[2][1]
    others = sections[-1][3]
    assert all(title in others for title, _p, _l in legal.LIBRARIES)
