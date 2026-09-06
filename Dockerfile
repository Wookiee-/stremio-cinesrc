FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7001 \
    SIDECAR_PORT=8001 \
    STREAM_MODE=raw \
    CINESRC_ENABLED=1 \
    CINESRC_URL=http://127.0.0.1:8001

# Node.js 22 (for the CineSrc sidecar)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sidecar ./sidecar
RUN npm ci --prefix sidecar --omit=dev

COPY app ./app
COPY run.py ./

EXPOSE 7001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; p=os.getenv('PORT','7001'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=5).status == 200 else 1)"

# addon supervises the sidecar (single process tree, one log stream)
CMD ["sh", "-c", "exec python run.py --with-sidecar"]
