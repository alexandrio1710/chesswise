# Works for Fly.io, Railway (Dockerfile-based deploys), or any other
# container host. Stockfish is a system binary, not a Python package, so
# it's installed via apt rather than pip.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

# The DB lives inside the container by default (chess_tracker.db, one
# level above app/) and is LOST on every redeploy/restart unless you mount
# a persistent volume and point DB_PATH at a path inside it — see the
# deployment section of the README before relying on this for real data.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "app"]
