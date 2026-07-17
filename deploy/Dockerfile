# OpenCharaAgent — one-click server image (plan Track E / Track F).
#
# We install the prebuilt WHEEL, which carries the built frontend (front/webui/)
# via package-data — so this image needs NO node and NO source/dist tree, just
# the wheel. Build the wheel first with `scripts/build-wheel.sh` (it emits
# dist/*.whl), then build this image from the repo root:
#
#   scripts/build-wheel.sh
#   docker build -f deploy/Dockerfile -t chara:latest .
#
# Single-stage python:3.12-slim, mirroring reference/AstrBot/Dockerfile but
# leaner (AstrBot copies source + installs node; we don't have to).
FROM python:3.12-slim

WORKDIR /app

# ripgrep backs the search_files tool; git is handy for chara terminal work;
# curl bootstraps NodeSource below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ripgrep \
        git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Browser tools (required env, owner 2026-06-19): Node + the agent-browser CLI +
# its own Chromium + Chromium's runtime libraries. We install the libs EXPLICITLY
# (python:3.12-slim is too bare; `agent-browser install --with-deps` does NOT pull
# them on slim, so Chrome would fail with `libglib-2.0.so.0: cannot open ...`).
# Chromium installs under /root/.agent-browser (outside the ~/.chara volume, so
# it bakes into the image).
#
# ISOLATION CAVEAT (Docker, owner 2026-06-19): inside Docker the chara's `sandbox`
# jail falls back to Landlock (Docker blocks the user namespaces bwrap needs).
# Landlock confines the SHELL fine, but agent-browser's Chrome currently crashes
# under Landlock (a Chrome/crashpad-under-Landlock issue still open — see
# docs/OPEN-WORK.md). Until that's resolved, for a JAILED browser in Docker run the
# container so bwrap works (userns enabled) OR deploy system-level (install.sh,
# bwrap — validated). Under `admin` isolation the browser works in-container today
# (the container is the boundary). The libs below make that path work.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g agent-browser \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxcb1 libxkbcommon0 libatspi2.0-0 libx11-6 libxcomposite1 \
        libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && agent-browser install \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy the prebuilt wheel(s) produced by scripts/build-wheel.sh. The wheel
# bundles front/webui/, so the served UI is inside the install — no extra steps.
# Install with the server + messaging extras (websockets/qrcode) straight from
# the wheel file so `chara desktop` and the gateways work out of the box.
COPY dist/*.whl /tmp/wheels/
RUN set -eux; \
    whl="$(ls /tmp/wheels/*.whl | head -n1)"; \
    python -m pip install --no-cache-dir "${whl}[server,messaging]"; \
    rm -rf /tmp/wheels

# Shim Chrome's crashpad handler to inject --database (Chrome-for-Testing headless
# omits it and the handler exits, killing Chrome under the in-container Landlock
# jail). Harmless where Chrome supplies its own. Required for the jailed browser
# in Docker (validated 2026-06-19).
RUN python -c "from chara.tools.builtin._browser_driver import ensure_crashpad_db_fix; ensure_crashpad_db_fix()"

# Sessions, cards and config live under ~/.chara — persist it via a volume.
ENV CHARA_HOME=/root/.chara
VOLUME ["/root/.chara"]

# The supervisor's HTTP renderer (6180) + WS gateway (6181 = http+1, the
# deterministic non-loopback default so a reverse proxy can pin it). Bound to
# 0.0.0.0 by the entrypoint; put a TLS reverse proxy in front past loopback (README).
EXPOSE 6180 6181

# The entrypoint generates an access token if CHARA_TOKEN is unset and prints
# it (the wildcard bind needs a token — the gate), then serves headless. Set
# CHARA_TOKEN to pin a known token; CHARA_PORT to change the port.
COPY deploy/entrypoint.sh /usr/local/bin/chara-entrypoint
RUN chmod +x /usr/local/bin/chara-entrypoint
ENTRYPOINT ["/usr/local/bin/chara-entrypoint"]
