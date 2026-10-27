# The FoxESS dashboard, in a container.
#
# The image holds the code and nothing else. The settings file - which
# carries the FoxESS password, the Open API key and the Octopus API key in
# plain text - is mounted in from the host at run time (see
# docker-compose.yml), so a rebuild can never lose it and a built image can
# never leak it.
#
# The dashboard is written against the Python standard library only, so
# there is nothing to install and nothing to compile: the image is the
# official Python one, plus the timezone database.

FROM python:3.13-slim

# The dashboard asks the OS for its timezone by name - Europe/London and
# friends - and the slim images ship without the timezone database. Without
# tzdata, ZoneInfo("Europe/London") raises ZoneInfoNotFoundError, which
# takes the clock and the solar forecast down with it.
#
# DEBIAN_FRONTEND=noninteractive and TZ stop tzdata from asking questions
# while the image is being built.
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# The dashboard does not need root. It needs to read the code, and to write
# one thing: the settings file it is handed.
#
# The uid is 1000 because that is the first ordinary user on a Raspberry Pi
# OS, so the mounted settings file usually belongs to the right person
# already. docker-compose.yml can override it with FOXESS_UID/FOXESS_GID.
RUN groupadd --gid 1000 foxess \
 && useradd --uid 1000 --gid 1000 --home-dir /home/foxess \
            --shell /usr/sbin/nologin foxess \
 && mkdir -p /home/foxess /config \
 && chown -R foxess:foxess /home/foxess /config

WORKDIR /app

# Only what the server needs: the package, the pages and the entry point.
# The tests, the preview images and the display clients stay out of it.
COPY --chown=root:root foxess/ ./foxess/
COPY --chown=root:root static/ ./static/
COPY --chown=root:root run.py ./run.py

# Where the settings live, and how to listen. run.py reads all three from
# the environment, so the container works whether it is started by compose
# or by hand.
ENV FOXESS_DASHBOARD_CONFIG=/config/config.json \
    FOXESS_HOST=0.0.0.0 \
    FOXESS_PORT=8765

EXPOSE 8765

USER foxess

# The liveness check. /healthz answers {"ok": true} as soon as the server is
# listening, and it needs nothing but the standard library, so there is no
# curl to install in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os,sys,urllib.request; p=os.environ.get('FOXESS_PORT','8765'); r=urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', timeout=4); sys.exit(0 if r.status==200 else 1)"

CMD ["python3", "run.py"]
