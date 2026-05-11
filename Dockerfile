# Telegram-side deployment for the wechat-collection app.
#
# Why this Dockerfile is separate from the SCF deployment:
#   SCF in mainland China cannot reach api.telegram.org (GFW blocks it).
#   We deploy the SAME codebase to Fly.io Singapore so the Telegram path
#   works. The Fly image uses standard pip install — none of the SCF
#   bundled-wheel gymnastics (packages/, scf_bootstrap) are needed here.
#
# Build / run locally:
#   docker build -t wechat-collection .
#   docker run --rm -p 8080:8080 --env-file .env wechat-collection
#
# Deploy to Fly:
#   fly deploy

# Python 3.10 specifically — pydantic 1.8.2 (pinned in requirements.txt for SCF
# compatibility) is broken on 3.11 due to a reserved-keyword check in inspect.
# SCF also runs 3.10, so this keeps both deployments on the same interpreter.
FROM python:3.10-slim

WORKDIR /app

# Install Python deps first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application source — .dockerignore excludes everything else
COPY config.py main.py ./
COPY routes/   ./routes/
COPY services/ ./services/
COPY models/   ./models/
COPY utils/    ./utils/

# Fly injects PORT; default to 8080 for local docker runs
ENV PORT=8080
EXPOSE 8080

# Run uvicorn directly. main.py's __main__ block isn't used here.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
