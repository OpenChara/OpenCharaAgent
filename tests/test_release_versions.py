"""Every place that states the product version agrees.

`chara.__version__` is what the updater compares against the latest GitHub
release tag (server/hub/updates.py), so a release that bumps pyproject but not
the package (or the reverse) makes every install report a phantom update or
never see a real one. The lockfiles and the SPA's package.json carry the same
number; release commits bumped some of these by hand and the rest drifted
(uv.lock sat at 0.2.0, the web lockfile at 0.1.13, through v0.2.2). The
Electron shell in apps/desktop is versioned on its own and is not pinned here.
"""
import json
import re
import tomllib

from chara.config import ROOT


def _package_version() -> str:
    text = (ROOT / "src" / "chara" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    assert m, "src/chara/__init__.py must define __version__"
    return m.group(1)


def test_release_versions_agree():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {p["name"]: p.get("version") for p in lock["package"]}

    web = json.loads((ROOT / "apps" / "web" / "package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((ROOT / "apps" / "web" / "package-lock.json").read_text(encoding="utf-8"))

    found = {
        "src/chara/__init__.py": _package_version(),
        "uv.lock": locked.get(pyproject["project"]["name"]),
        "apps/web/package.json": web["version"],
        "apps/web/package-lock.json": web_lock["version"],
        "apps/web/package-lock.json packages['']": web_lock["packages"][""]["version"],
    }
    drift = {where: v for where, v in found.items() if v != version}
    assert not drift, f"pyproject.toml is {version}; these disagree: {drift}"
