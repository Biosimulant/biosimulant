# Local disposable harness only. Build with --platform linux/amd64.
FROM python:3.12.3-slim@sha256:afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends libxml2 git \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir uv==0.13.4 build==1.6.1 pytest==9.1.1 pytest-cov==7.1.0
WORKDIR /work/biosim
