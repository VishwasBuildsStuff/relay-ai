FROM python:3.11.18-slim-trixie@sha256:ffe4b0c76963e64f05484d74dbb3f67fad1effcf3c5c22c2235f0fafc8a9c98f

LABEL org.opencontainers.image.title="RELAY AI sandbox"
LABEL org.opencontainers.image.base.name="python:3.11.18-slim-trixie"
LABEL org.opencontainers.image.base.digest="sha256:ffe4b0c76963e64f05484d74dbb3f67fad1effcf3c5c22c2235f0fafc8a9c98f"

# Build this image before starting the API. Runtime test containers have no network.
RUN python -m pip install --no-cache-dir --disable-pip-version-check "pytest==8.3.5"
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
