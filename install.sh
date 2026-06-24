#!/usr/bin/env bash
# LunaMoth installer (macOS / Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/Lunamos/LunaMoth/main/install.sh | bash
#
# Two channels:
#
#   * USER (default) — install the prebuilt WHEEL from the latest GitHub Release
#     via `uv tool install`. The wheel bundles the built frontend (front/webui/),
#     so there's no node build and no source checkout. Update later with
#     `lunamoth update` (uv tool upgrade).
#
#   * DEV/edge — `LUNAMOTH_CHANNEL=dev` (or `--dev`) keeps the old git-checkout
#     layout: a clone in $LUNAMOTH_HOME/app + `uv sync`. Developers have node and
#     rebuild the served UI with `cd apps/web && npm run build`. Update later with
#     `lunamoth update` (git pull + uv sync).
#
# A PRIVATE repo: the release-asset download needs a token — set
# GITHUB_TOKEN=<a PAT with repo:read> before running (used as a bearer header).
set -euo pipefail

REPO_SLUG="${LUNAMOTH_REPO_SLUG:-Lunamos/LunaMoth}"
REPO_URL="${LUNAMOTH_REPO:-https://github.com/${REPO_SLUG}.git}"
LUNAMOTH_HOME="${LUNAMOTH_HOME:-$HOME/.lunamoth}"
APP_DIR="$LUNAMOTH_HOME/app"
BIN_DIR="$LUNAMOTH_HOME/bin"
LINK_DIR="${LUNAMOTH_LINK_DIR:-$HOME/.local/bin}"
CHANNEL="${LUNAMOTH_CHANNEL:-user}"

# --dev / --channel dev flag (works with `… | bash -s -- --dev`).
for arg in "$@"; do
  case "$arg" in
    --dev|--channel=dev) CHANNEL="dev" ;;
    --user|--channel=user) CHANNEL="user" ;;
  esac
done

say()  { printf '\033[1;36m[lunamoth]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[lunamoth]\033[0m %s\n' "$*" >&2; exit 1; }

# Browser tools are a LunaMoth environment requirement (owner 2026-06-19): the
# Node `agent-browser` CLI + its own Chromium back the browser_* tools, which now
# run under the default `sandbox` isolation too. Best-effort + non-fatal: a failed
# browser setup never blocks the core install. On Linux+apt we bootstrap Node 20
# (NodeSource) if missing; on macOS we point at `brew install node`.
browser_setup() {
  say "setting up browser tools (agent-browser + Chromium) ..."
  if ! command -v npm >/dev/null 2>&1; then
    if [ "$(uname -s)" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
      say "  Node.js not found — installing Node 20 (NodeSource) ..."
      curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 \
        && apt-get install -y nodejs >/dev/null 2>&1 || true
    fi
  fi
  if command -v npm >/dev/null 2>&1; then
    if npm install -g agent-browser >/dev/null 2>&1 \
       && agent-browser install --with-deps >/dev/null 2>&1; then
      say "  browser tools ready (agent-browser + Chromium)"
    else
      say "  NOTE: browser setup incomplete — finish later with: lunamoth setup browser"
    fi
  else
    say "  NOTE: Node.js (node+npm) not found — the browser_* tools need it."
    say "        Install Node 18+ ($([ "$(uname -s)" = Darwin ] && echo 'brew install node' || echo 'your package manager')), then: lunamoth setup browser"
  fi
}

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) fail "unsupported platform $(uname -s) (macOS and Linux only for now)" ;;
esac

mkdir -p "$LUNAMOTH_HOME" "$BIN_DIR" "$LINK_DIR"

# --- uv: prefer system uv, else install a managed copy into $BIN_DIR --------
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  if [ ! -x "$BIN_DIR/uv" ]; then
    say "installing uv into $BIN_DIR ..."
    installer="$(mktemp)"
    curl -fsSL https://astral.sh/uv/install.sh -o "$installer" || fail "could not download uv installer"
    UV_UNMANAGED_INSTALL="$BIN_DIR" sh "$installer" >/dev/null || fail "uv install failed"
    rm -f "$installer"
  fi
  UV="$BIN_DIR/uv"
fi
say "using uv: $UV"

# ===========================================================================
# DEV / edge channel — git checkout + uv sync (the old default).
# ===========================================================================
if [ "$CHANNEL" = "dev" ]; then
  command -v git >/dev/null 2>&1 || fail "git is required for the dev channel (macOS: xcode-select --install; Linux: apt/dnf install git)"
  if [ -d "$APP_DIR/.git" ]; then
    say "updating existing checkout at $APP_DIR ..."
    git -C "$APP_DIR" pull --ff-only origin main || fail "git pull failed (local changes? see $APP_DIR)"
  else
    say "cloning $REPO_URL -> $APP_DIR ..."
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
  fi

  say "syncing python environment ..."
  # server + messaging extras so `lunamoth desktop` (needs websockets) and
  # `lunamoth gateway` (qrcode/websockets) work out of the box.
  (cd "$APP_DIR" && "$UV" sync -q --extra server --extra messaging) || fail "uv sync failed"

  SHIM="$LINK_DIR/lunamoth"
  cat > "$SHIM" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/.venv/bin/lunamoth" "\$@"
EOF
  chmod +x "$SHIM"
  say "installed dev shim: $SHIM"
  say "NOTE: dev channel — rebuild the served UI after frontend edits:"
  say "  cd $APP_DIR/apps/web && npm ci && npm run build"

  case ":$PATH:" in
    *":$LINK_DIR:"*) ;;
    *) say "NOTE: $LINK_DIR is not on your PATH. Add this to your shell profile:"
       say "  export PATH=\"$LINK_DIR:\$PATH\"" ;;
  esac
  browser_setup
  say "done (dev channel). run: lunamoth"
  exit 0
fi

# ===========================================================================
# USER channel — install the prebuilt wheel from the latest GitHub Release.
# ===========================================================================
command -v curl >/dev/null 2>&1 || fail "curl is required"

API="https://api.github.com/repos/${REPO_SLUG}/releases/latest"
AUTH_HEADER=()
if [ -n "${GITHUB_TOKEN:-}" ]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
fi

say "resolving the latest release of ${REPO_SLUG} ..."
release_json="$(curl -fsSL ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} -H "Accept: application/vnd.github+json" "$API")" \
  || fail "could not fetch the latest release of ${REPO_SLUG} — no published release yet, or a private repo (set GITHUB_TOKEN=<repo:read PAT>). To install from source instead, re-run with: ... | bash -s -- --dev"

# Find the .whl asset's download URL (grep/sed — no jq dependency).
WHEEL_URL="$(printf '%s\n' "$release_json" \
  | grep -oE '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]+\.whl"' \
  | head -n1 \
  | sed -E 's/.*"(https[^"]+\.whl)"/\1/')"
[ -n "$WHEEL_URL" ] || fail "no .whl asset found in the latest release of ${REPO_SLUG}"
say "wheel: $WHEEL_URL"

# A private-repo asset isn't a public URL — download it through the API with the
# token, then install the local file. A public release URL installs directly.
INSTALL_TARGET="$WHEEL_URL"
TMP_WHEEL=""
if [ -n "${GITHUB_TOKEN:-}" ]; then
  TMP_WHEEL="$(mktemp -t lunamoth-XXXXXX).whl"
  say "downloading wheel (authenticated) ..."
  curl -fsSL ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} -H "Accept: application/octet-stream" -o "$TMP_WHEEL" "$WHEEL_URL" \
    || fail "wheel download failed"
  INSTALL_TARGET="$TMP_WHEEL"
fi

# Integrity: if the release publishes a checksum for the wheel, verify the
# downloaded bytes before installing — this product hands an LLM a shell, so a
# tampered wheel is the worst case. If no checksum is published we say so plainly
# rather than implying the download was verified.
if [ -n "$TMP_WHEEL" ]; then
  WHEEL_BASENAME="${WHEEL_URL##*/}"
  SUM_URL="$(printf '%s' "$release_json" \
    | grep -oE '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]+(SHA256SUMS|\.sha256)"' \
    | head -n1 | sed -E 's/.*"(https[^"]+)"/\1/')"
  if [ -n "$SUM_URL" ]; then
    if command -v sha256sum >/dev/null 2>&1; then
      ACTUAL_SHA="$(sha256sum "$TMP_WHEEL" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
      ACTUAL_SHA="$(shasum -a 256 "$TMP_WHEEL" | awk '{print $1}')"
    else
      ACTUAL_SHA=""
    fi
    if [ -n "$ACTUAL_SHA" ]; then
      EXPECTED_SHA="$(curl -fsSL ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} -H "Accept: application/octet-stream" "$SUM_URL" \
        | grep -oiE '[0-9a-f]{64}' | head -n1)"
      if [ -n "$EXPECTED_SHA" ] && [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
        rm -f "$TMP_WHEEL"
        fail "wheel checksum MISMATCH (expected $EXPECTED_SHA, got $ACTUAL_SHA) — refusing to install"
      fi
      [ -n "$EXPECTED_SHA" ] && say "wheel checksum verified ($ACTUAL_SHA)"
    fi
  else
    say "NOTE: this release publishes no checksum — wheel integrity NOT verified."
  fi
fi

say "installing lunamoth (server + messaging extras) ..."
# `uv tool install` puts an isolated venv under uv's data dir and links the
# `lunamoth` entrypoint onto PATH. Re-running upgrades in place (--force).
"$UV" tool install --force "lunamoth[server,messaging] @ ${INSTALL_TARGET}" \
  || fail "uv tool install failed"

[ -n "$TMP_WHEEL" ] && rm -f "$TMP_WHEEL"

# `uv tool install` links into uv's own bin dir; surface it on PATH if needed.
UV_BIN="$("$UV" tool dir --bin 2>/dev/null || true)"
if [ -n "$UV_BIN" ]; then
  case ":$PATH:" in
    *":$UV_BIN:"*) ;;
    *) say "NOTE: $UV_BIN is not on your PATH. Add this to your shell profile:"
       say "  export PATH=\"$UV_BIN:\$PATH\""
       say "  (or run: $UV tool update-shell)" ;;
  esac
fi

browser_setup
say "done. run: lunamoth"
