# Isolated Linux Virtual Screen Worker for Agent WASP
# Provides on-demand per-bot Xvfb + Openbox + persistent Chrome profile + live noVNC/voxl stream.
# Strictly isolated: zero access to host secrets, Redis, Postgres, or Agent WASP Vault.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    MAX_SCREENS=2 \
    SCREEN_BASE=100 \
    SCREEN_SIZE=1280x800 \
    SCREEN_IDLE_MINUTES=15 \
    PROFILES_DIR=/data/profiles \
    SCREENS_PORT=6900

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    python3 \
    ca-certificates \
    curl \
    xvfb \
    openbox \
    x11-utils \
    dbus-x11 \
    at-spi2-core \
    x11vnc \
    novnc \
    websockify \
    fonts-noto-core \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Non-root sandbox user for browser profiles and screens
RUN useradd -m -s /bin/bash waspbot && \
    mkdir -p /app /data/profiles /tmp/wasp-screens && \
    chown -R waspbot:waspbot /app /data /tmp/wasp-screens

WORKDIR /app
COPY --chown=waspbot:waspbot screen_daemon.py /app/screen_daemon.py

USER waspbot
EXPOSE 6900 6901 6902

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6900/health', timeout=3)"

CMD ["python3", "/app/screen_daemon.py"]
