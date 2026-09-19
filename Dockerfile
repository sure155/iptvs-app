FROM python:3.11-alpine

LABEL org.opencontainers.image.source="https://github.com/sure155/iptvs-app" \
      org.opencontainers.image.description="IPTV HLS Reverse Proxy - lightweight, dependency-free" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "-u", "iptv_proxy.py"]
