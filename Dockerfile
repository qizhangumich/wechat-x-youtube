# Telegram-side deployment for the wechat-collection app.
#
# Currently runs on Hetzner (Germany) under Caddy reverse proxy. Originally
# built for Fly.io Singapore (the GFW blocks api.telegram.org from Tencent
# SCF) but Hetzner replaced Fly to consolidate on existing infrastructure.
#
# Build / run locally:
#   docker build -t wechat-collection .
#   docker run --rm -p 8080:8080 --env-file .env \
#     -v $(pwd)/video-output:/data/video-output wechat-collection
#
# Deploy to Hetzner:
#   git pull && docker build -t wechat-x-youtube . && docker stop wechat-x-youtube && \
#   docker rm wechat-x-youtube && docker run -d --name wechat-x-youtube \
#   --restart=always --network n8n_default --env-file /root/wechat-x-youtube/.env \
#   -v /root/video-output:/data/video-output wechat-x-youtube

# Python 3.10 specifically — pydantic 1.8.2 (pinned in requirements.txt for SCF
# compatibility) is broken on 3.11 due to a reserved-keyword check in inspect.
# SCF also runs 3.10, so this keeps both deployments on the same interpreter.
FROM python:3.10-slim

# System packages: ffmpeg is required by yt-dlp to merge separate video+audio
# streams (YouTube's DASH delivery splits them above 360p). curl is handy for
# in-container debugging.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp needs FREQUENT updates because YouTube/X/etc. constantly change their
# anti-scraping. The ARG below busts Docker's layer cache for this RUN whenever
# you build with --build-arg YTDLP_REBUILD=$(date +%s). Without that flag,
# Docker will re-use the cached layer (saving time but using stale yt-dlp).
ARG YTDLP_REBUILD=cache
RUN pip install --no-cache-dir --upgrade yt-dlp \
    && yt-dlp --version

# Copy only the application source — .dockerignore excludes everything else
COPY config.py main.py ./
COPY routes/   ./routes/
COPY services/ ./services/
COPY models/   ./models/
COPY utils/    ./utils/

# Where downloaded media files land. Mount a host volume here at runtime
# (e.g. -v /root/video-output:/data/video-output) so files persist across
# container restarts and can be retrieved via SFTP from the host path.
RUN mkdir -p /data/video-output

ENV PORT=8080
EXPOSE 8080

# Hardcoded --port 8080 (NOT ${PORT}) because shell substitution from --env-file
# was unreliable: a stray empty PORT= line in .env caused uvicorn to fall back
# to its 8000 default, which Caddy couldn't reach. Always 8080 here, period.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
