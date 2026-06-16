#!/usr/bin/env bash
# Build a LunaMoth wheel that BUNDLES the built frontend SPA.
#
# The frontend (apps/web/) is built at packaging time into the gitignored
# src/lunamoth/front/webui/, then `python -m build` (via uv) packs that dir into
# the wheel through [tool.setuptools.package-data] (pyproject.toml). The result
# is a single .whl in dist/ that carries the prebuilt UI — no node and no source
# needed at install time. This is hermes-agent's model (web/ → web_dist/ →
# package-data), adapted to setuptools (which has no build hook, so we simply
# build the frontend FIRST so the files exist on disk at build time).
#
# Idempotent + CI-friendly: re-running rebuilds cleanly. CI runs this on a tagged
# release and uploads dist/*.whl as a GitHub Release asset (plan §2.4 / Track F).
#
#   scripts/build-wheel.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$ROOT/apps/web"
WEBUI_DIR="$ROOT/src/lunamoth/front/webui"
DIST_DIR="$ROOT/dist"

say() { printf '\033[1;36m[build-wheel]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[build-wheel]\033[0m %s\n' "$*" >&2; exit 1; }

command -v npm >/dev/null 2>&1 || fail "npm is required to build the frontend"

# --- 1. build the SPA into the gitignored webui/ ----------------------------
say "building frontend (apps/web -> src/lunamoth/front/webui) ..."
cd "$WEB_DIR"
# `npm ci` needs a lockfile; fall back to `npm install` for a fresh tree.
if [ -f package-lock.json ]; then
  npm ci
else
  npm install
fi
npm run build

[ -f "$WEBUI_DIR/index.html" ] || fail "frontend build produced no $WEBUI_DIR/index.html"
say "frontend built: $WEBUI_DIR"

# --- 2. build the wheel (carries webui/ via package-data) --------------------
cd "$ROOT"
# Start clean so the assertion below picks the wheel we just built.
rm -rf "$DIST_DIR"

if command -v uv >/dev/null 2>&1; then
  say "building wheel with uv ..."
  uv build --wheel
else
  say "uv not found; building wheel with python -m build ..."
  python -m build --wheel
fi

# --- 3. assert the wheel actually bundled the UI -----------------------------
say "verifying the wheel bundles front/webui/ ..."
python - <<'PY'
import glob, sys, zipfile
wheels = sorted(glob.glob("dist/*.whl"))
if not wheels:
    sys.exit("no wheel produced in dist/")
whl = wheels[-1]
names = zipfile.ZipFile(whl).namelist()
if not any(n.endswith("front/webui/index.html") or "front/webui/index.html" in n for n in names):
    sys.exit(f"FAIL: {whl} does not contain front/webui/index.html\n  sample: {names[:20]}")
print(f"WHEEL OK: webui bundled in {whl}")
PY

say "done. wheel(s):"
ls -1 "$DIST_DIR"/*.whl
