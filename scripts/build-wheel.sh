#!/usr/bin/env bash
# Build a OpenCharaAgent wheel that BUNDLES the built frontend SPA.
#
# The frontend (apps/web/) is built at packaging time into the gitignored
# src/chara/front/webui/, then `python -m build` (via uv) packs that dir into
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
WEBUI_DIR="$ROOT/src/chara/front/webui"
DIST_DIR="$ROOT/dist"

say() { printf '\033[1;36m[build-wheel]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[build-wheel]\033[0m %s\n' "$*" >&2; exit 1; }

# Portable python: many environments (incl. uv-managed ones + CI) have no bare
# `python` on PATH — fall back to python3, then `uv run python`.
run_python() {
  if command -v python3 >/dev/null 2>&1; then python3 "$@"
  elif command -v python >/dev/null 2>&1; then python "$@"
  else uv run python "$@"; fi
}

command -v npm >/dev/null 2>&1 || fail "npm is required to build the frontend"

# --- 1. build the SPA into the gitignored webui/ ----------------------------
say "building frontend (apps/web -> src/chara/front/webui) ..."
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

# --- 1b. bundle the repo-root content dirs into the package ------------------
# cards/ + toolpacks/ live at the repo ROOT (not under src/chara/), so a wheel
# wouldn't otherwise carry them — and a wheel install has no repo root, so the
# runtime (config.content_dir) would find no toolpacks → every chara loses its
# tools, and no cards → no bundled personas. Copy them into the package as
# _bundled/, shipped via [tool.setuptools.package-data]. (2026-06-17 deploy fix.)
BUNDLED_DIR="$ROOT/src/chara/_bundled"
say "bundling cards/ + toolpacks/ + skills/ into $BUNDLED_DIR ..."
rm -rf "$BUNDLED_DIR"
mkdir -p "$BUNDLED_DIR"
for d in cards toolpacks skills; do
  [ -d "$ROOT/$d" ] || fail "repo content dir $ROOT/$d is missing"
  cp -R "$ROOT/$d" "$BUNDLED_DIR/$d"
done
[ -n "$(ls -A "$BUNDLED_DIR/toolpacks"/*.json 2>/dev/null)" ] || fail "no toolpacks/*.json bundled"
[ -n "$(find "$BUNDLED_DIR/skills" -name SKILL.md 2>/dev/null)" ] || fail "no skills/**/SKILL.md bundled"

# --- 2. build the wheel (carries webui/ + _bundled/ via package-data) --------
cd "$ROOT"
# Start clean. The egg-info SOURCES.txt + an old build/ tree make setuptools
# resurrect files that have since been DELETED from the source — a stale build/
# once re-bundled a removed module. Wipe all three so the wheel reflects only the
# current tree.
rm -rf "$DIST_DIR" "$ROOT/build"
find "$ROOT/src" -name "*.egg-info" -type d -exec rm -rf {} + 2>/dev/null || true

if command -v uv >/dev/null 2>&1; then
  say "building wheel with uv ..."
  uv build --wheel
else
  say "uv not found; building wheel with python -m build ..."
  run_python -m build --wheel
fi

# --- 3. assert the wheel actually bundled the UI + content -------------------
say "verifying the wheel bundles front/webui/ + cards/ + toolpacks/ ..."
run_python - <<'PY'
import glob, sys, zipfile
wheels = sorted(glob.glob("dist/*.whl"))
if not wheels:
    sys.exit("no wheel produced in dist/")
whl = wheels[-1]
names = zipfile.ZipFile(whl).namelist()
def need(pred, what):
    if not any(pred(n) for n in names):
        sys.exit(f"FAIL: {whl} missing {what}\n  sample: {names[:20]}")
need(lambda n: "front/webui/index.html" in n, "front/webui/index.html")
need(lambda n: "_bundled/toolpacks/" in n and n.endswith(".json"), "_bundled/toolpacks/*.json")
need(lambda n: "_bundled/cards/" in n, "_bundled/cards/")
need(lambda n: "_bundled/skills/" in n and n.endswith("SKILL.md"), "_bundled/skills/**/SKILL.md")
print(f"WHEEL OK: webui + cards + toolpacks + skills bundled in {whl}")
PY

# --- 4. emit a checksum manifest so installs can verify integrity -----------
# install.sh fetches a SHA256SUMS release asset and refuses on mismatch; the
# release workflow uploads this file alongside the wheel. Generated here so a
# local build produces the same artifact CI publishes.
say "writing dist/SHA256SUMS ..."
( cd "$DIST_DIR"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum *.whl > SHA256SUMS
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 *.whl > SHA256SUMS
  else
    fail "no sha256sum/shasum available to write SHA256SUMS"
  fi )

say "done. wheel(s):"
ls -1 "$DIST_DIR"/*.whl "$DIST_DIR"/SHA256SUMS
