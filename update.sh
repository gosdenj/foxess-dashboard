#!/usr/bin/env bash
#
# Update the dashboard.
#
#   ./update.sh              pull the newest code, build it, put it in place
#   ./update.sh --no-pull    rebuild from the code already on this machine
#   ./update.sh --help
#
# What it does, in order:
#
#   1. git pull --ff-only        the newest code, if there is a remote
#   2. docker compose build      a new image, from the code just pulled
#   3. tag the running image     so there is something to go back to
#   4. docker compose up -d      the new image, in place of the old one
#   5. wait for /healthz         if it never answers, roll back
#
# The settings file is not involved in any of this: it lives in ./config on
# the host, and the container mounts it. An update cannot lose it.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

SERVICE="foxess-dashboard"
CONFIG_DIR="${FOXESS_CONFIG_DIR:-$REPO/config}"
CONFIG_FILE="$CONFIG_DIR/config.json"
HEALTH_TIMEOUT=120

# The settings directory may be given as a relative path, and the container
# needs it spelled out in full.
case "$CONFIG_DIR" in
    /*) ;;
    *) CONFIG_DIR="$REPO/$CONFIG_DIR" ;;
esac

PULL=1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  x %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,18p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-pull) PULL=0; shift ;;
        -h|--help) usage ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

# -- the tools -----------------------------------------------------------

command -v docker >/dev/null 2>&1 || die "docker is not installed"
docker info >/dev/null 2>&1 || die "docker is not running (or you are not in the docker group)"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    die "docker compose is not installed"
fi

# -- 1. the newest code --------------------------------------------------

if [ "$PULL" = "1" ]; then
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        die "this is not a git checkout, so there is nothing to pull"
    fi
    if [ -z "$(git remote)" ]; then
        cat >&2 <<'EOF'

  x This repository has no remote, so there is nowhere to pull the newest
    code from.

    Add one, and push what is here to it:

        git remote add origin https://github.com/<you>/foxess-dashboard.git
        git push -u origin main

    Then run this script again. If you would rather not do that yet,
    './update.sh --no-pull' rebuilds from the code on this machine.
EOF
        exit 1
    fi

    before=$(git rev-parse --short HEAD)
    say "Pulling the newest code"
    if ! git pull --ff-only; then
        die "git pull failed - the local changes may be in the way (git status)"
    fi
    after=$(git rev-parse --short HEAD)
    if [ "$before" = "$after" ]; then
        info "already up to date ($after)"
    else
        info "$before -> $after"
    fi
fi

# -- the image name ------------------------------------------------------

IMAGE="$("${COMPOSE[@]}" config --images 2>/dev/null | head -n1 || true)"
[ -n "$IMAGE" ] || IMAGE="foxess-dashboard:latest"

# The image to go back to, named after the one that is running.
PREVIOUS_IMAGE="${IMAGE%:*}:previous"

# -- 2. the new image ----------------------------------------------------

say "Building the image"
info "image: $IMAGE"
# The registry is sometimes briefly unreachable, and a failed build is not
# worth losing patience over: the running container is untouched until the
# new image is ready, so it is safe to try again.
built=0
for attempt in 1 2 3; do
    if "${COMPOSE[@]}" build; then
        built=1
        break
    fi
    if [ "$attempt" -lt 3 ]; then
        warn "the build failed - trying again in 10s (attempt $attempt of 3)"
        sleep 10
    fi
done
if [ "$built" != "1" ]; then
    die "the build failed. The container that is running now has not been touched."
fi

# -- 3. remember what is running now --------------------------------------

say "Remembering the running image"
running=$("${COMPOSE[@]}" ps -q "$SERVICE" 2>/dev/null | head -n1 || true)
if [ -z "$running" ]; then
    running=$("${COMPOSE[@]}" ps -q -a "$SERVICE" 2>/dev/null | head -n1 || true)
fi
if [ -n "$running" ]; then
    old_image=$(docker inspect --format '{{.Image}}' "$running" 2>/dev/null || true)
    if [ -n "$old_image" ]; then
        docker tag "$old_image" "$PREVIOUS_IMAGE"
        info "the running image is now $PREVIOUS_IMAGE"
    fi
else
    info "nothing is running yet - this is the first start"
fi

# -- the settings file must be writable by the container ------------------

check_config() {
    [ -f "$CONFIG_FILE" ] || {
        warn "no settings file yet - the dashboard will start, and you can"
        warn "fill in the credentials on the settings page"
        return 0
    }
    # The uid the container will run as, from the compose file.
    local user
    user=$("${COMPOSE[@]}" config 2>/dev/null \
        | sed -n 's/^[[:space:]]*user:[[:space:]]*//p' | head -n1 | tr -d '"' || true)
    local uid gid
    case "$user" in
        *:*) uid="${user%%:*}"; gid="${user##*:}" ;;
        "")   uid=1000; gid=1000 ;;
        *)    uid="$user"; gid="$user" ;;
    esac
    if ! docker run --rm --user "$uid:$gid" -v "$CONFIG_DIR:/config" \
            --entrypoint python3 "$IMAGE" -c \
            'import os,sys; sys.exit(0 if os.access("/config", os.W_OK) else 1)' \
            >/dev/null 2>&1; then
        cat >&2 <<EOF

  x The container runs as $uid:$gid and cannot write to $CONFIG_DIR.

    The dashboard saves the settings you change on the settings page, so it
    needs to be able to write there. Either give it the directory:

        sudo chown -R $uid:$gid $CONFIG_DIR

    or let it run as you, by putting this in a .env file next to
    docker-compose.yml:

        FOXESS_UID=$(id -u)
        FOXESS_GID=$(id -g)

EOF
        exit 1
    fi
    info "the settings file is writable"
}

say "Checking the settings"
check_config

# -- 4. the new container -------------------------------------------------

say "Starting the new container"
"${COMPOSE[@]}" up -d --no-build --force-recreate

cid=$("${COMPOSE[@]}" ps -q "$SERVICE" 2>/dev/null | head -n1 || true)
if [ -z "$cid" ]; then
    cid=$("${COMPOSE[@]}" ps -q -a "$SERVICE" 2>/dev/null | head -n1 || true)
fi
[ -n "$cid" ] || cid="$SERVICE"

# -- 5. did it come up? ---------------------------------------------------

wait_for_health() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT))
    local health state restarts
    while [ "$SECONDS" -lt "$deadline" ]; do
        health=$(docker inspect --format \
            '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "$cid" 2>/dev/null || echo gone)
        case "$health" in
            healthy) return 0 ;;
            unhealthy) return 1 ;;
            none)
                # No health check on this image: running is good enough.
                state=$(docker inspect --format '{{.State.Running}}' "$cid" 2>/dev/null || echo false)
                [ "$state" = "true" ] && return 0
                return 1
                ;;
            gone) return 1 ;;
        esac
        # The container is set to restart itself, so a crash shows up as a
        # restart counter going up. Waiting the full time for a container
        # that is never going to start helps nobody.
        state=$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null || echo gone)
        restarts=$(docker inspect --format '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)
        if [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
            return 1
        fi
        if [ "${restarts:-0}" -ge 3 ] 2>/dev/null; then
            return 1
        fi
        sleep 2
    done
    return 1
}

say "Waiting for it to answer"
if wait_for_health; then
    build=$(docker exec "$cid" python3 -c \
        'import json, os, urllib.request; p = os.environ.get("FOXESS_PORT", "8765"); print(json.load(urllib.request.urlopen("http://127.0.0.1:" + p + "/api/state", timeout=5)).get("build", "?"))' \
        2>/dev/null || echo "?")
    info "healthy, build $build"
    say "Done."
    host_ip=""
    if hostname -I >/dev/null 2>&1; then
        host_ip=$(hostname -I | awk '{print $1}')
    elif command -v ipconfig >/dev/null 2>&1; then
        host_ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
    fi
    if [ -n "$host_ip" ]; then
        info "the dashboard is at http://$host_ip:${FOXESS_PORT:-8765}/"
    else
        info "the dashboard is at http://<this-machine>:${FOXESS_PORT:-8765}/"
    fi
    exit 0
fi

# -- it did not come up: go back ------------------------------------------

warn "the new container did not come up - going back to the old one"
docker logs --tail 30 "$cid" 2>&1 | sed 's/^/    /' || true

if docker image inspect "$PREVIOUS_IMAGE" >/dev/null 2>&1; then
    docker tag "$PREVIOUS_IMAGE" "$IMAGE"
    "${COMPOSE[@]}" up -d --no-build --force-recreate
    say "Rolled back to the previous image"
    back=$("${COMPOSE[@]}" ps -q "$SERVICE" 2>/dev/null | head -n1 || true)
    [ -n "$back" ] || back=$SERVICE
    docker logs --tail 10 "$back" 2>&1 | sed 's/^/    /' || true
    exit 1
fi

die "there is no previous image to go back to - the old one was never tagged"
