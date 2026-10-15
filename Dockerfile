FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

LABEL org.opencontainers.image.source="https://github.com/egeback/traefik-dns-sync"
LABEL org.opencontainers.image.description="Sync Traefik Docker labels to DNS providers"
LABEL org.opencontainers.image.licenses="MIT"

RUN useradd --create-home --shell /bin/bash app
WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/traefik-dns-sync /usr/local/bin/traefik-dns-sync

RUN mkdir -p /data && chown app:app /data
VOLUME /data

USER app

ENTRYPOINT ["traefik-dns-sync"]
