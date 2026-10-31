# The fingerprint service (jplay/fingerprint): your music collection as a Shazam for the house, with Panako.
# Built by .github/workflows/fingerprint.yml -> ghcr.io/jpvaneerde/jplayer-fingerprint; deployed with Argo CD.
#   docker build -f deploy/fingerprint/Dockerfile -t jplayer-fingerprint .
FROM python:3.12-slim-bookworm

# Java runs Panako; ffmpeg decodes the music for it (FLAC, MP3, ...)
RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jre-headless ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Panako 2.1 (https://github.com/JorenSix/Panako), the published jar, checked against its checksum
ARG PANAKO_URL=https://github.com/JorenSix/Panako/releases/download/joss/Panako-2.1-all.jar
ARG PANAKO_SHA256=767cdd2cd0991658c4a25a0b8e887f9a2a38f69ae17781b02fe1652e1a7173d4
RUN mkdir -p /opt/panako \
 && curl -fsSL -o /opt/panako/panako.jar "$PANAKO_URL" \
 && echo "$PANAKO_SHA256  /opt/panako/panako.jar" | sha256sum -c -

RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "mutagen>=1.47"

WORKDIR /app
COPY jplay/__init__.py jplay/__init__.py
COPY jplay/fingerprint jplay/fingerprint

ENV DATA_DIR=/data \
    MUSIC_DIR=/music \
    PORT=8080 \
    PANAKO_JAR=/opt/panako/panako.jar \
    TMPDIR=/tmp \
    TZ=Europe/Stockholm \
    PYTHONUNBUFFERED=1
RUN useradd -u 1000 -m jplay && mkdir -p /data && chown jplay:jplay /data
USER 1000
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "jplay.fingerprint"]
