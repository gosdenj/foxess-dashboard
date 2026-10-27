# Deploying with Portainer

Portainer runs the dashboard as a **stack** — a Docker Compose file it
deploys and keeps up to date for you.

```
  ┌──────────────────┐    HTTP     ┌───────────────────────┐    HTTPS    ┌───────────────┐
  │  Pi screen(s)    │ ──────────► │  the stack            │ ──────────► │ FoxESS cloud  │
  │  kiosk mode      │ ◄────────── │  on the target host   │ ◄────────── │ Octopus       │
  └──────────────────┘   /events   └───────────────────────┘             └───────────────┘
                                      │  reads and writes
                                      ▼
                            the foxess-config volume
                          (the settings, and the secrets in them)
```

There are three pieces, and it is worth knowing which is which:

| Piece | Where it lives | Who changes it |
|---|---|---|
| **The image** | GitHub Container Registry | The GitHub workflow, on every push |
| **The stack** | `docker-compose.portainer.yml` in the repository | You, in a commit |
| **The settings** | The `foxess-config` Docker volume | You, on the settings page |

## The thing that catches people out

**Portainer redeploys a stack without rebuilding it.** Under the hood it
runs `docker-compose up -d` — with no `--build`. So a redeploy updates the
*compose file* but keeps the *image it already has*, and a new commit
would not reach the running container.

That is why the image comes from the registry rather than from the
repository:

```
   push to main
        │
        ▼
   the GitHub workflow builds the image ──► ghcr.io/…/foxess-dashboard:latest
        │
        ▼
   Portainer notices the repository has changed, re-pulls the image
        │
        ▼
   the new version is running
```

The workflow is [`.github/workflows/docker.yml`](../.github/workflows/docker.yml)
and it is already part of the repository. It signs in with the workflow's
own token, so there is no secret to set up.

## Before you start

The repository is private, so Portainer needs a credential to clone it,
and the image in the registry is private too.

| What | Where the credential goes |
|---|---|
| Cloning the repository | The stack's **Authentication → Basic** fields (a GitHub token) |
| Pulling the image | A **registry** in Portainer, if the package is private |

**A GitHub token** (classic) with:

- `repo` — to clone the private repository
- `read:packages` — to pull the image, if the package is private

A **deploy key** (an SSH key) works for the clone instead of a token, and
is tidier: it is scoped to this one repository. Portainer's git stack takes
a private key.

**The package can be made public**, which removes the registry credential
entirely: *Package settings → Change visibility → Public*. The source code
stays private; only the built image becomes readable by anyone who pulls
it. The image contains no secrets — the settings are not in it — but the
choice is yours.

## 1. The stack

**Stacks → Add stack → Git repository**

| Field | Value |
|---|---|
| Name | `foxess` |
| Repository URL | `https://github.com/gosdenj/foxess-dashboard.git` |
| Repository reference name | `refs/heads/main` |
| Compose path | `docker-compose.portainer.yml` |
| Authentication | Basic — a GitHub token, if the repository is private |
| Environment variables | `FOXESS_PORT` if the host port should not be 8765 |
| **Pull latest image** | **on** |
| **Poll repository for changes** | **on**, every 5 minutes |

The last two are what make it automatic. *Pull latest image* is the
re-pull of the registry image; without it, Portainer would redeploy the
old image and nothing would change.

Deploy it. The first deployment pulls the image, creates the volume and
starts the container.

### The same thing over the API

If you would rather not click through the form:

```bash
curl -X POST "http://minix.local:9000/api/stacks?type=2&method=compose&endpointId=1" \
  -H "Authorization: Bearer $PORTAINER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "Name": "foxess",
    "RepositoryURL": "https://github.com/gosdenj/foxess-dashboard.git",
    "RepositoryReferenceName": "refs/heads/main",
    "RepositoryAuthentication": true,
    "RepositoryUsername": "gosdenj",
    "RepositoryPassword": "<a GitHub token>",
    "ComposeFile": "docker-compose.portainer.yml",
    "AutoUpdate": {
      "Interval": "5m",
      "ForceUpdate": false,
      "ForcePullImage": true
    },
    "Env": []
  }'
```

`type=2` is a Docker Compose stack, `method=compose` is a standalone
Docker environment rather than Swarm, and `endpointId` is the environment
in **Endpoints**.

## 2. The settings

The settings file — the FoxESS password, the Open API key, the Octopus
key — lives in a **named Docker volume**, `foxess-config`, mounted at
`/config`.

A volume rather than a folder on the host, for three reasons:

- it is the **same** wherever the container runs, with no path to get wrong
- it **survives a redeploy** — a redeploy replaces the container, not the
  volume
- it does not depend on the host and the container sharing a filesystem,
  which they do not if Portainer is a container with Docker Desktop
  underneath it

The volume takes its ownership from the image, where `/config` belongs to
uid 1000 — the uid the container runs as. So the dashboard can write to it
without anybody having to `chown` anything.

To fill the settings in, open **http://<target-ip>:8765/settings** and
save them. The first save writes the file into the volume.

To read them back:

```bash
docker run --rm -v foxess-config:/config ghcr.io/gosdenj/foxess-dashboard:latest \
  cat /config/config.json
```

### If the volume ever gets the wrong ownership

```bash
docker run --rm -u 0 -v foxess-config:/config alpine chown -R 1000:1000 /config
```

## 3. The updates

With polling on, Portainer checks the repository every five minutes. When
the commit has changed, it pulls the new image and recreates the container.

**There is no rollback.** That is the one thing Portainer does not do that
`update.sh` does. If a new version breaks the dashboard, the way back is
to check out the last good commit and let Portainer redeploy it:

```bash
git revert <the bad commit>
git push
```

or, from the stack's page, **Pull and redeploy** after moving `main` back
to the last good commit.

## 4. The screens

The Pi screens are clients: a browser and an address.

```bash
python3 display/pi_kiosk.py --url http://<target-ip>:8765 --no-server
```

`--no-server` says "the server is somewhere else" — which is the whole
point here: the stack serves the page, and the Pi just shows it.

## The catch

**The dashboard has no authentication.** Anything that can reach the port
can read the page, change the settings, and ask the server for the
password and the API keys in plain text (`/api/settings/export`).

On a box with a public address, do not publish the port. Keep it on the
LAN, put a password in front of it, or reach it over SSH:

```bash
ssh -L 8765:127.0.0.1:8765 you@your-server
```

## When something goes wrong

**The stack will not deploy** — the container's logs say why. The usual
one is the image: `docker pull ghcr.io/gosdenj/foxess-dashboard:latest` on
the target will say whether it is a credentials problem or a missing
package.

**The settings will not save** — the container cannot write to the volume.
See *If the volume ever gets the wrong ownership* above.

**The dashboard is empty** — no credentials yet. Open the settings page
and fill them in; the serial number and the power station are found
automatically from the Open API key.

**A new commit did not arrive** — check that the workflow ran (the
*Actions* tab in the repository), that the image was published (the
*Packages* tab), and that *Pull latest image* is on in the stack.
