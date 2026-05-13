# ── Stage 1: Build Tailwind CSS ──────────────────────────────────────────────
# Replaces the Tailwind Play CDN (<script src="https://cdn.tailwindcss.com">)
# with a pre-compiled, minified, static CSS file. This eliminates 'unsafe-eval'
# from the Content-Security-Policy.
FROM node:20-alpine AS tailwind-builder
WORKDIR /build
COPY tailwind-build/package.json tailwind-build/package-lock.json* ./
RUN npm ci --prefer-offline 2>/dev/null || npm install
COPY tailwind-build/input.css ./
# Copy templates and app.js so Tailwind can scan class names
COPY src/dashboard/templates/ ./templates/
COPY src/dashboard/static/app.js ./
# Write inline config with paths matching the Docker build layout
RUN printf 'module.exports={content:["./templates/**/*.html","./app.js"],plugins:[require("daisyui")],daisyui:{themes:["dark","light"],logs:false}}\n' > tailwind.config.js
RUN npx tailwindcss -i input.css -o tailwind.css --minify

# ── Stage 2: Runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget jq git \
    chromium chromium-driver \
    fonts-liberation fonts-noto-color-emoji \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI static binary (no daemon needed — uses host socket)
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-27.4.0.tgz" \
    | tar xz --strip-components=1 -C /usr/local/bin docker/docker

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

RUN groupadd -g 1000 agent && useradd -u 1000 -g agent -m agent
# Match host docker GID (988) so agent can use docker.sock
RUN groupadd -g 988 docker-host || true && usermod -aG docker-host agent

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY tests/ ./tests/

# Seed prime.md / prime.default.md into image. Copied to /data/config on first
# boot by main.py if missing — gives the agent a working persona out of the box
# without a manual config step.
COPY config/ ./config/

# Strip dev artefacts that may have leaked into src/. The COPY itself can't
# exclude with rsync-style globs, so we delete them post-copy. Keeps the image
# clean across rebuilds even if a developer left stray .bak/.orig files.
RUN find /app/src /app/tests -type f \( -name '*.bak' -o -name '*.orig' -o -name '*~' \) -delete

# Inject the compiled Tailwind CSS from the build stage
COPY --from=tailwind-builder /build/tailwind.css ./src/dashboard/static/css/tailwind.css

# ── Policy regression gate ───────────────────────────────────────────────
# These tests pin down behaviors that prime.md operational directives promise.
# A change that breaks any of them is a regression — fix the gate, not the
# test. Build fails loudly here rather than silently in production.
RUN pip install --no-cache-dir pytest pytest-asyncio \
    && python -m pytest tests/test_policy_regressions.py --tb=short -q

RUN chown -R agent:agent /app

USER agent

CMD ["python", "-m", "src.main"]
