# Running the dashboard in Docker

The dashboard is written against the Python standard library only, so putting
it in a container is mostly a matter of choosing a base image and being
careful about where the secrets live.

```
  ┌──────────────────┐    HTTP     ┌───────────────────────┐    HTTPS    ┌───────────────┐
  │  Pi screen(s)    │ ──────────► │  the container        │ ──────────► │ FoxESS cloud  │
  │  kiosk mode      │ ◄────────── │  on the host          │ ◄────────── │ Octopus       │
  └──────────────────┘   /events   └───────────────────────┘             └───────────────┘
                                      │  reads and writes
                                      ▼
                                ./config/config.json
                          (the settings, and the secrets in them)
```

One container does all the work: it holds the FoxESS session, the Open API
key and the Octopus credentials, and it serves the page to every screen. The
screens are dumb terminals — they need a browser and an address, nothing
else. The ESP32 client works the same way.

The one rule that matters: **the settings file is never in the image.** It
lives in a directory on the host and is mounted into the container, so a
rebuild can never lose it and a built image can never leak it.

---

## What you need

- **Docker** with `docker compose` (v2) on the machine that holds the
  credentials — usually the Pi, or any box on the LAN
- **This repository**, checked out
- **Nothing else.** There are no packages to install: the image is the
  official Python one plus the timezone database

The image builds for `linux/amd64`, `linux/arm64` (Pi 4, Pi 5) and
`linux/arm/v7` (Pi 3), so the same Dockerfile serves every machine.

---

## First time

```bash
git clone <your-repo> foxess-dashboard
cd foxess-dashboard

# The settings live here, on the host, and are mounted into the container.
mkdir -p config
cp config.example.json config/config.json
chmod 600 config/config.json

docker compose up -d --build
```

Then open **http://<host-ip>:8765/settings** and fill in your credentials —
the FoxESS email and password for the live feed, the Open API key for the
totals, and the Octopus keys for the meter. The inverter serial and the power
station id are found automatically.

The dashboard keeps running if you never fill them in; it just shows nothing
much.

### The settings file

| File | What it is |
|---|---|
| `config/config.json` | The settings, with the password and the API keys in plain text. Written `0600`. |
| `config/config-history.json` | The half-hourly history the dashboard records as it runs. |

Both are in `.gitignore`, and neither is ever copied into the image.

The whole **directory** is mounted, not just the file, because the dashboard
writes the history next to the settings and replaces the settings file
atomically — both of which need a real directory.

To keep the settings somewhere else, put it in a `.env` file next to
`docker-compose.yml`:

```
FOXESS_CONFIG_DIR=/etc/foxess-dashboard
```

### Who owns the settings file

The container runs as uid 1000 — the first ordinary user on Raspberry Pi OS —
so on most machines the settings file already belongs to the right person.
If yours does not, the dashboard can still read it but cannot save changes,
and it will say so.

Give it the directory:

```bash
sudo chown -R 1000:1000 config
```

or let it run as you, in a `.env` file:

```
FOXESS_UID=$(id -u)
FOXESS_GID=$(id -g)
```

---

## On a hosted server

The same three commands work on a hosted Intel (x86_64) box — a VPS, a
droplet, an EC2 instance — as long as Docker is installed. The image is built
for the machine it is built on, so nothing special is needed, and the base
image publishes `linux/amd64`, `linux/arm64` and `linux/arm/v7`: the same
Dockerfile serves a VPS, a Pi and a Mac.

```bash
git clone <your-repo> foxess-dashboard
cd foxess-dashboard
mkdir -p config && cp config.example.json config/config.json
docker compose up -d --build
```

### Open the port in two places

A hosted instance has a firewall in front of it: the provider's (DigitalOcean
firewall, AWS security group, and so on) **and** the one on the box
(`ufw`, `firewalld`). Both need to allow the port.

```bash
sudo ufw allow 8765/tcp
```

### The one that matters

**A hosted instance has a public IP, and the dashboard has no
authentication.** Anything that can reach the port can read the page, change
the settings, and ask the server for the password and the API keys in plain
text (`/api/settings/export` returns them).

So do not open the port to the world. Three ways to be safe:

1. **Publish nothing; reach it over SSH.** The dashboard stays on the box:

   ```bash
   ssh -L 8765:127.0.0.1:8765 you@your-server
   ```

   It is then at `http://127.0.0.1:8765/` on your own machine, and the Pi
   screens reach it over the same tunnel, a VPN or a private network.

2. **Put a password in front of it.** nginx or Caddy in front of the
   container, with TLS and a password, forwarding to 8765.

3. **Keep it on a private network.** A hosted instance on Tailscale, WireGuard
   or a VPC has no public route to it at all.

The container does not know the difference between these; it serves the same
page either way.

### If you are root on the box

The container runs as uid 1000, and the settings directory you just created
belongs to root. `./update.sh` stops before it replaces anything and tells
you:

```
sudo chown -R 1000:1000 config
```

or set `FOXESS_UID` and `FOXESS_GID` in `.env` to match a user you have.

### Building on one machine for another

The image is built for the machine it is built on. If you build on an Apple
Silicon Mac and want it to run on an Intel server, ask for that platform and
send it over — the image is about 120 MB:

```bash
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose build
docker save foxess-dashboard:latest | ssh you@your-server docker load
```

Then, on the server:

```bash
docker compose up -d --no-build
```

Building on the server itself is simpler, and needs neither — the image is
built for the machine it is running on.

---

## Updating

```bash
./update.sh
```

That is the whole update path. The script:

1. **pulls** the newest code (`git pull --ff-only`)
2. **builds** a new image from it
3. **tags** the image that is running now as `foxess-dashboard:previous`
4. **replaces** the container with the new one
5. **waits** for `/healthz` to answer

If the new container does not come up within two minutes, it puts the old
image back and starts it again, and prints the log of what went wrong. A
failed build never touches the container that is running, so a bad update
costs you a reboot of nothing.

### It needs a remote

The script pulls from git, so the repository needs a remote:

```bash
git remote add origin https://github.com/<you>/foxess-dashboard.git
git push -u origin main
```

Until then, `./update.sh` will say so and stop. `./update.sh --no-pull`
rebuilds from whatever is on the machine, which is useful when you are
editing the code yourself.

### Rolling back by hand

The previous image is always kept:

```bash
docker images foxess-dashboard        # :latest and :previous
docker tag foxess-dashboard:previous foxess-dashboard:latest
docker compose up -d --force-recreate --no-build
```

### Handy commands

| What | Command |
|---|---|
| Follow the log | `docker compose logs -f` |
| Restart it | `docker compose restart` |
| Stop it | `docker compose down` |
| Which build is running | `docker compose exec foxess-dashboard python3 -c "import urllib.request,json; print(json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/state'))['build'])"` |
| The settings it is using | `docker compose exec foxess-dashboard cat /config/config.json` |

---

## The screens

The Pi screens are clients: they do not need the credentials, the git
checkout or Docker. They need a browser and the address of the server.

```bash
git clone <your-repo> foxess-dashboard
cd foxess-dashboard
python3 display/pi_kiosk.py --url http://<host-ip>:8765 --no-server
```

`--no-server` says "the server is somewhere else" — which is the whole point
here: the container serves the page, and the Pi just shows it.

For an unattended boot, use a systemd unit on the Pi:

```ini
[Unit]
Description=FoxESS dashboard (kiosk)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/foxess-dashboard
Environment=DISPLAY=:0
ExecStart=/usr/bin/python3 /home/pi/foxess-dashboard/display/pi_kiosk.py --url http://192.168.1.50:8765 --no-server --window-size 800,480
Restart=always
RestartSec=5

[Install]
WantedBy=graphical.target
```

See [`display/README.md`](../display/README.md) for the whole kiosk setup —
the screensaver, the mouse cursor, the fonts and the Pi-specific Chromium
flags.

---

## Security

**The dashboard has no authentication.** Anyone who can reach the port can
read the page, change the settings, and ask the server for the settings in
plain text (`/api/settings/export` returns the password and the API keys).

That is a deliberate choice for a private LAN. It means:

- **Keep the port on the LAN.** Do not publish it to the internet, and do not
  put it behind a port-forward on the router.
- **The settings file is the crown jewels.** It has the FoxESS password, the
  Open API key and the Octopus API key in it. Keep it `0600`, and keep it out
  of git — which is why it is not in the image either.
- If you need it off the LAN, put a reverse proxy with a password in front
  of it. The dashboard will not do it for you.

---

## Trying it without credentials

The demo mode feeds made-up numbers into the same UI, so you can see the
container working before you have any keys:

```bash
docker compose run --rm -p 8899:8765 foxess-dashboard python3 run.py --demo
# -> http://127.0.0.1:8899
```

---

## When something goes wrong

**"port is already allocated"** — something else is on 8765. It is often a
copy of the dashboard running outside Docker; stop it, or move the container
to another port with `FOXESS_PORT=8766` in `.env`.

**The settings will not save** — the container cannot write to the settings
directory. `./update.sh` checks this before it replaces anything and tells
you the fix; see *Who owns the settings file* above.

**The container is unhealthy** — `docker compose logs`. The dashboard prints
the reason there: no API key, a failed login, a wrong serial number.

**It is slow to start** — the first build downloads the base image and
installs the timezone database, which needs the internet. After that the
build works offline.

**A new release and the container disagree** — the build stamp is in the
page's footer and in `/api/state`. If it is not what you expect, the
container is running an old image: `docker compose up -d --build`.
