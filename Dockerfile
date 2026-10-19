FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local

# ffmpeg нужен для склейки видео и звука, Deno - для YouTube (yt-dlp запускает через него JS).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
 && curl -fsSL https://deno.land/install.sh | sh \
 && apt-get purge -y --auto-remove unzip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py start.sh ./

CMD ["sh", "start.sh"]
