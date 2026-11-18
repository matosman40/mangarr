# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MANGARR_LIBRARY=/library MANGARR_CONFIG=/config MANGARR_PORT=8080 TZ=UTC
COPY --from=builder /install /usr/local
WORKDIR /app
COPY mangarr ./mangarr
# Démarre en root pour réparer les droits des volumes, puis passe à PUID:PGID (1000 par défaut)
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /library /config
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
VOLUME ["/library", "/config"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('MANGARR_PORT','8080'), timeout=4)" || exit 1
CMD ["python", "-m", "mangarr", "serve"]
