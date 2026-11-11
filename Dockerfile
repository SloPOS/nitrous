FROM python:3.12-slim

LABEL org.opencontainers.image.title="Nitrous" \
      org.opencontainers.image.description="Web-based Oxide/uMod plugin manager for Rust dedicated servers" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY LICENSE NOTICE ./

ENV OXIDE_DIR=/oxide DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
