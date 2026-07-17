#!/bin/sh
# OpenCharaAgent container entrypoint. The supervisor refuses a 0.0.0.0 bind without a
# token (the access gate). For a turn-key `docker compose up`, generate one if the
# operator didn't supply CHARA_TOKEN, and print it so `docker compose logs`
# shows how to authenticate. cmd_desktop honors the CHARA_TOKEN env.
set -e

PORT="${CHARA_PORT:-6180}"
WS_PORT=$((PORT + 1))   # the supervisor's non-loopback default (http+1)

if [ -z "${CHARA_TOKEN:-}" ]; then
  CHARA_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
  export CHARA_TOKEN
  GENERATED=1
fi

echo "============================================================"
if [ "${GENERATED:-0}" = "1" ]; then
  echo " OpenCharaAgent: generated an access token (set CHARA_TOKEN to pin it)"
else
  echo " OpenCharaAgent: using the provided CHARA_TOKEN"
fi
echo " token: ${CHARA_TOKEN}"
echo " direct (publish both ports ${PORT}+${WS_PORT}):"
echo "   http://<this-host>:${PORT}/#token=${CHARA_TOKEN}&ws=${WS_PORT}"
if [ -n "${CHARA_ALLOW_HOST:-}" ]; then
  echo " behind your TLS reverse proxy (allow-host=${CHARA_ALLOW_HOST}):"
  echo "   https://${CHARA_ALLOW_HOST}/#token=${CHARA_TOKEN}"
else
  echo " behind a TLS reverse proxy: set CHARA_ALLOW_HOST=<your-domain> so the"
  echo "   Host/Origin allowlist accepts it, then open https://<your-domain>/#token=…"
fi
if [ -n "${CHARA_PASSWORD:-}" ]; then
  echo " password login: enabled (CHARA_PASSWORD) — bookmark the bare URL and sign in"
else
  echo " password login: a strong one is generated on first start and printed ONCE below"
  echo "   (set CHARA_PASSWORD to choose your own; only its hash is stored in auth.json)"
fi
echo " (never expose ${PORT}/${WS_PORT} directly past loopback — see README)"
echo "============================================================"

# A non-loopback bind's Host/Origin allowlist is loopback + the bound host only;
# a reverse proxy forwards the PUBLIC domain, so it must be allow-listed explicitly.
set -- chara desktop --host 0.0.0.0 --port "${PORT}" --no-open
if [ -n "${CHARA_ALLOW_HOST:-}" ]; then
  set -- "$@" --allow-host "${CHARA_ALLOW_HOST}"
fi
exec "$@"
