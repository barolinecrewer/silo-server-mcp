FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SILO_TRANSPORT=http SILO_HOST=0.0.0.0 SILO_PORT=8000 SILO_CACHE_DIR=/tmp/silo-server-mcp
RUN python -m pip install --upgrade "pip>=26.2" \
 && python -m pip install "mcp==1.30.0" "httpx==0.28.1" "jsonschema>=4.20,<5" \
 && useradd --system --uid 10001 silo
WORKDIR /app
COPY --chmod=644 server.py .
USER silo
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)"
CMD ["python", "server.py"]
