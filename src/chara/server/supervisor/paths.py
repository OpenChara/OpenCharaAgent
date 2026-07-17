"""Filesystem roots the supervisor serves from / spawns children in.

Kept in one leaf module so every submodule (children, http, the coordinator)
and the de-facto ``supervisor.APP_DIR`` / ``supervisor.WEB_DIR`` package
attributes agree byte-for-byte. NOTE the path depth: this file is
``src/chara/server/supervisor/paths.py`` so the repo root is parents[4]
(it was parents[3] from the old flat ``server/supervisor.py``).
"""
from __future__ import annotations

from pathlib import Path

# Repo root (…/OpenCharaAgent). The serve --stdio children are spawned with this cwd.
APP_DIR = Path(__file__).resolve().parents[4]
# The built React SPA (apps/web → `npm run build`). Gitignored, bundled into the
# wheel via package-data; `cd apps/web && npm run build` regenerates it in dev.
WEB_DIR = Path(__file__).resolve().parents[2] / "front" / "webui"
UPLOAD_MAX = 8 * 1024 * 1024
