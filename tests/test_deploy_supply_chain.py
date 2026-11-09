"""deploy/: supply-chain pins for obsidian-headless (npm) and uv.

Static checks only: the committed npm lock must pin every package with a
sha512 integrity, and provision.sh must pin the uv tarball digests for its
default version instead of running an installer script.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parents[1] / "deploy"
OBH = DEPLOY / "ob-headless"
PROVISION = DEPLOY / "provision.sh"

# Packages allowed to run install scripts in the unprivileged build. A new
# entry after a bump must be reviewed before being added here.
ALLOWED_INSTALL_SCRIPTS = {"node_modules/better-sqlite3"}


@pytest.fixture(scope="module")
def package() -> dict:
    return json.loads((OBH / "package.json").read_text())


@pytest.fixture(scope="module")
def lock() -> dict:
    return json.loads((OBH / "package-lock.json").read_text())


def test_package_json_pins_exact_version(package: dict) -> None:
    assert package["private"] is True
    version = package["dependencies"]["obsidian-headless"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version


def test_lock_matches_package_json(package: dict, lock: dict) -> None:
    assert lock["lockfileVersion"] >= 2
    root = lock["packages"][""]
    assert root["dependencies"] == package["dependencies"]
    version = package["dependencies"]["obsidian-headless"]
    assert lock["packages"]["node_modules/obsidian-headless"]["version"] == version


def test_every_locked_package_has_sha512_integrity(lock: dict) -> None:
    entries = {k: v for k, v in lock["packages"].items() if k}
    assert "node_modules/better-sqlite3" in entries
    for name, entry in entries.items():
        assert entry.get("integrity", "").startswith("sha512-"), name
        assert entry.get("resolved", "").startswith("https://registry.npmjs.org/"), name


def test_only_reviewed_packages_have_install_scripts(lock: dict) -> None:
    with_scripts = {k for k, v in lock["packages"].items() if v.get("hasInstallScript")}
    assert with_scripts <= ALLOWED_INSTALL_SCRIPTS, with_scripts - ALLOWED_INSTALL_SCRIPTS


def test_provision_pins_uv_sha256_for_default_version() -> None:
    text = PROVISION.read_text()
    default = re.search(r'^UV_VERSION="\$\{UV_VERSION:-([0-9.]+)\}"$', text, re.M)
    assert default, "UV_VERSION default not found"
    for arch in ("aarch64", "x86_64"):
        pin = re.search(
            rf"^\s*{re.escape(default.group(1))}/{arch}\) echo ([0-9a-f]+) ;;$", text, re.M
        )
        assert pin, f"no pinned sha256 for uv {default.group(1)}/{arch}"
        assert len(pin.group(1)) == 64


def test_provision_has_no_unverified_installers() -> None:
    code = (
        line for line in PROVISION.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    text = "\n".join(code)
    assert "astral.sh/uv" not in text
    assert "npm install -g" not in text
    assert "npm ci --omit=dev" in text
