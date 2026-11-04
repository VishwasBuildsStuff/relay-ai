FROM python:3.11-slim

# Build this image before starting the API. Runtime test containers have no network.
RUN pip install --no-cache-dir pytest==8.3.5
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
