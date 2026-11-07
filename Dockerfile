FROM python:3.11

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY scrape_artist_images.py ./
COPY webapp.py ./
COPY toplist_data.py ./
COPY lx_source_runner.js ./
COPY templates ./templates
COPY static ./static
COPY docker-entrypoint.sh ./
COPY config.example.json /app/default-config.json
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
