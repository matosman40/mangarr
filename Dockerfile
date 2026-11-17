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
# Utilisateur non root (UID/GID 1000 par défaut ; surchargeable avec `user:` dans compose)
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin mangarr \
    && mkdir -p /library /config && chown mangarr:mangarr /library /config
USER mangarr
VOLUME ["/library", "/config"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('MANGARR_PORT','8080'), timeout=4)" || exit 1
CMD ["python", "-m", "mangarr", "serve"]
