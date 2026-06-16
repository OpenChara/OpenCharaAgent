# LunaMoth — one-click server image (plan Track E / Track F).
#
# We install the prebuilt WHEEL, which carries the built frontend (front/webui/)
# via package-data — so this image needs NO node and NO source/dist tree, just
# the wheel. Build the wheel first with `scripts/build-wheel.sh` (it emits
# dist/*.whl), then build this image from the repo root:
#
#   scripts/build-wheel.sh
#   docker build -f deploy/Dockerfile -t lunamoth:latest .
#
# Single-stage python:3.12-slim, mirroring reference/AstrBot/Dockerfile but
# leaner (AstrBot copies source + installs node; we don't have to).
FROM python:3.12-slim

WORKDIR /app

# ripgrep backs the search_files tool; git is handy for chara terminal work.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        ripgrep \
        git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy the prebuilt wheel(s) produced by scripts/build-wheel.sh. The wheel
# bundles front/webui/, so the served UI is inside the install — no extra steps.
# Install with the server + messaging extras (websockets/qrcode) straight from
# the wheel file so `lunamoth desktop` and the gateways work out of the box.
COPY dist/*.whl /tmp/wheels/
RUN set -eux; \
    whl="$(ls /tmp/wheels/*.whl | head -n1)"; \
    python -m pip install --no-cache-dir "${whl}[server,messaging]"; \
    rm -rf /tmp/wheels

# Sessions, cards and config live under ~/.lunamoth — persist it via a volume.
ENV LUNAMOTH_HOME=/root/.lunamoth
VOLUME ["/root/.lunamoth"]

# The supervisor's HTTP renderer + WS gateway. Bound to 0.0.0.0 below; put a
# TLS reverse proxy in front for anything past loopback (see README).
EXPOSE 6180

# Serve headless: bind all interfaces, never try to open a local browser.
# (Requires `--host` on the desktop subcommand — added in Track D.)
CMD ["lunamoth", "desktop", "--host", "0.0.0.0", "--port", "6180", "--no-open"]
