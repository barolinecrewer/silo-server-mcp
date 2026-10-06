FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SILO_TRANSPORT=http SILO_HOST=0.0.0.0 SILO_PORT=8000 SILO_CACHE_DIR=/tmp/silo-server-mcp
WORKDIR /app
COPY --chmod=644 server.py .
RUN --mount=from=ghcr.io/astral-sh/uv:0.12,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache -r server.py \
 && useradd --system --uid 10001 silo
USER silo
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)"
CMD ["python", "server.py"]
