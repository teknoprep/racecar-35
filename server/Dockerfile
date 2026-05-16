# racecar-35 cloud receiver — single-file FastAPI service.
#
# Build:   docker build -t racecar35-cloud .
# Run:     docker run --rm -p 8089:8089 -v $(pwd)/data:/data racecar35-cloud
# Compose: docker compose up --build
FROM python:3.12-slim

# Speed/quietness tuning for python + pip
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

# Install deps first to keep this layer cacheable across app changes.
COPY app/requirements.txt /srv/app/requirements.txt
RUN pip install --no-cache-dir -r /srv/app/requirements.txt

# Then copy app source.
COPY app /srv/app

# Persistent volume for saved sessions. The compose file mounts ./data here.
ENV RACECAR_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8089

# --proxy-headers tells uvicorn to trust X-Forwarded-* headers from nginx.
# --forwarded-allow-ips="*" because nginx will sit on the host network and
# the source IP from inside the container will be the docker bridge address.
# If you want stricter, set RACECAR_TRUSTED_PROXIES in compose.
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8089", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
