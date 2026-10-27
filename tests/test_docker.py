"""The Docker setup.

The dashboard runs in a container, and the container is the piece that holds
the credentials, so the rules are narrow and worth checking:

- the settings file is mounted in from the host, never baked into the image
- the image has the timezone database, or the clock and the solar forecast
  are wrong
- the container does not run as root
- an update that breaks the container puts the old one back

These tests read the files as text. They are not a substitute for running
the thing, but they catch the mistakes that would only show up on the Pi.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "docker-compose.yml"
PORTAINER_COMPOSE = ROOT / "docker-compose.portainer.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "docker.yml"
UPDATE = ROOT / "update.sh"
GITIGNORE = ROOT / ".gitignore"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestTheImage(unittest.TestCase):
    """The Dockerfile."""

    def setUp(self) -> None:
        self.text = read(DOCKERFILE)

    def test_it_is_a_python_image(self):
        self.assertRegex(self.text, r"(?m)^FROM python:\d")

    def test_it_installs_the_timezone_database(self):
        """ZoneInfo needs it, and the slim images do not have it."""
        packages = " ".join(re.findall(r"apt-get install[^\n]*", self.text))
        self.assertIn("tzdata", packages)
        self.assertNotRegex(self.text, r"(?m)^\s*RUN pip install")

    def test_it_installs_nothing_else(self):
        """The dashboard is standard library only."""
        self.assertNotIn("pip install", self.text)
        self.assertNotIn("requirements.txt", self.text)

    def test_the_settings_file_is_not_copied_in(self):
        """The image must never carry the password or the API keys."""
        for line in self.text.splitlines():
            if line.strip().startswith("COPY"):
                self.assertNotIn("config.json", line)
                self.assertNotIn("config/", line)

    def test_it_copies_only_what_the_server_needs(self):
        for wanted in ("foxess/", "static/", "run.py"):
            self.assertIn(wanted, self.text)

    def test_it_does_not_run_as_root(self):
        self.assertRegex(self.text, r"(?m)^USER \w+")

    def test_the_user_is_not_root(self):
        users = re.findall(r"(?m)^USER (\S+)", self.text)
        self.assertTrue(users)
        self.assertNotIn("root", users)
        self.assertNotIn("0", users)

    def test_the_dashboard_asks_for_a_timezone(self):
        """Which is why the image needs the timezone database at all."""
        source = (ROOT / "foxess" / "weather.py").read_text(encoding="utf-8")
        self.assertIn("ZoneInfo(", source)

    def test_it_serves_on_every_interface(self):
        """Inside the container, the port mapping has to be able to reach it."""
        self.assertRegex(self.text, r"FOXESS_HOST=0\.0\.0\.0")

    def test_the_settings_are_read_from_the_mounted_directory(self):
        self.assertRegex(self.text, r"FOXESS_DASHBOARD_CONFIG=/config/config\.json")

    def test_it_answers_the_health_check(self):
        self.assertIn("HEALTHCHECK", self.text)
        self.assertIn("/healthz", self.text)

    def test_it_uses_no_third_party_packages(self):
        """Nothing to install means nothing to go wrong at build time."""
        self.assertNotRegex(self.text, r"(?m)^\s*RUN pip")


class TestTheIgnoreList(unittest.TestCase):
    """The .dockerignore."""

    def setUp(self) -> None:
        self.text = read(DOCKERIGNORE)
        self.lines = [line.strip() for line in self.text.splitlines()]
        self.patterns = [line for line in self.lines
                         if line and not line.startswith("#")]

    def test_the_settings_file_is_kept_out(self):
        self.assertIn("config.json", self.patterns)
        self.assertIn("config/", self.patterns)
        self.assertIn("config-history.json", self.patterns)

    def test_the_git_directory_is_kept_out(self):
        self.assertIn(".git", self.patterns)

    def test_the_history_file_is_kept_out(self):
        self.assertIn("*-history.json", self.patterns)


class TestTheComposeFile(unittest.TestCase):
    """docker-compose.yml."""

    def setUp(self) -> None:
        self.text = read(COMPOSE)

    def test_it_mounts_a_directory_for_the_settings(self):
        """A file would break the atomic replace and the history file."""
        self.assertRegex(self.text, r":/config\b")
        self.assertNotRegex(self.text, r"config\.json:/config")

    def test_the_mounted_directory_is_on_the_host(self):
        self.assertIn("./config", self.text)

    def test_it_publishes_a_port(self):
        self.assertRegex(self.text, r"(?m)^\s*-\s*\"?\$\{FOXESS_PORT:-8765\}:8765")

    def test_it_comes_back_after_a_reboot(self):
        self.assertIn("restart: unless-stopped", self.text)

    def test_it_does_not_run_as_root(self):
        self.assertRegex(self.text, r"(?m)^\s*user:\s*\"?\$\{FOXESS_UID:-1000\}")

    def test_the_log_cannot_fill_the_disk(self):
        self.assertIn("max-size", self.text)
        self.assertIn("max-file", self.text)


class TestThePortainerComposeFile(unittest.TestCase):
    """docker-compose.portainer.yml.

    Portainer redeploys a stack with `docker-compose up -d`, and there is
    no `--build` in it.  So the image has to come from the registry, and
    the settings have to live in a volume that a redeploy does not touch.
    """

    def setUp(self) -> None:
        self.text = read(PORTAINER_COMPOSE)

    def test_the_image_comes_from_the_registry(self):
        """A stack redeploy does not build, so it must be able to pull."""
        self.assertIn("ghcr.io/", self.text)
        self.assertRegex(self.text, r"(?m)^\s*image:\s*ghcr\.io/\S+:latest")

    def test_it_does_not_try_to_build(self):
        """A build section would be a fallback that hides a broken pull."""
        self.assertNotIn("build:", self.text)
        self.assertNotIn("context:", self.text)

    def test_the_settings_live_in_a_named_volume(self):
        """A folder on the host would depend on the host and the
        container sharing a filesystem, which they do not."""
        self.assertRegex(self.text, r"(?m)^\s*-\s*foxess-config:/config")
        self.assertNotIn("./config", self.text)

    def test_the_volume_has_a_fixed_name(self):
        """Otherwise it would be named after the stack."""
        self.assertRegex(self.text, r"(?m)^\s*name:\s*foxess-config")

    def test_the_port_is_the_same(self):
        self.assertRegex(self.text, r"(?m)^\s*-\s*\"?\$\{FOXESS_PORT:-8765\}:8765")

    def test_it_comes_back_after_a_reboot(self):
        self.assertIn("restart: unless-stopped", self.text)

    def test_it_does_not_run_as_root(self):
        self.assertRegex(self.text, r"(?m)^\s*user:\s*\"?\$\{FOXESS_UID:-1000\}")

    def test_the_log_cannot_fill_the_disk(self):
        self.assertIn("max-size", self.text)
        self.assertIn("max-file", self.text)

    def test_it_agrees_with_the_command_line_compose_file(self):
        """Two compose files, one dashboard: the parts they share must match."""
        other = read(COMPOSE)
        for field in ("FOXESS_DASHBOARD_CONFIG", "FOXESS_HOST",
                      "FOXESS_PORT", "TZ"):
            self.assertIn(field, self.text)
            self.assertIn(field, other)
        for phrase in ("restart: unless-stopped", "max-size", "max-file",
                       "container_name: foxess-dashboard"):
            self.assertIn(phrase, self.text)
            self.assertIn(phrase, other)


class TestThePublishWorkflow(unittest.TestCase):
    """.github/workflows/docker.yml.

    The image has to be published somewhere, because Portainer does not
    build it.
    """

    def setUp(self) -> None:
        self.text = read(WORKFLOW)

    def test_it_builds_on_a_push_to_main(self):
        self.assertIn("push:", self.text)
        self.assertRegex(self.text, r"branches:\s*\[main\]")

    def test_it_pushes_to_the_registry(self):
        self.assertIn("ghcr.io", self.text)
        self.assertIn("push: true", self.text)

    def test_it_signs_in_with_its_own_token(self):
        """So there is no secret to set up."""
        self.assertIn("secrets.GITHUB_TOKEN", self.text)

    def test_it_is_allowed_to_write_packages(self):
        """Anchored to the line, so a mention in a comment does not count."""
        self.assertRegex(self.text, r"(?m)^\s*packages:\s*write\s*$")

    def test_it_cannot_do_anything_else(self):
        self.assertRegex(self.text, r"(?m)^\s*contents:\s*read\s*$")

    def test_it_builds_for_every_architecture(self):
        """A hosted Intel server, an Apple Silicon Mac, a Pi 4 or 5, a Pi 3."""
        for platform in ("linux/amd64", "linux/arm64", "linux/arm/v7"):
            self.assertIn(platform, self.text)

    def test_it_holds_no_secret(self):
        self.assertNotRegex(self.text, r"(?i)(password|token|secret)\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}")


    def test_the_settings_are_not_in_the_image(self):
        """Nothing may set the secrets as environment variables in the file."""
        for line in self.text.splitlines():
            if "PASSWORD" in line or "API_KEY" in line:
                self.fail(f"a secret in the compose file: {line.strip()}")


class TestTheUpdateScript(unittest.TestCase):
    """update.sh."""

    def setUp(self) -> None:
        self.text = read(UPDATE)

    def test_it_pulls_the_newest_code(self):
        self.assertIn("git pull --ff-only", self.text)

    def test_it_says_so_when_there_is_no_remote(self):
        """Otherwise the failure is a mystifying git error."""
        self.assertIn("git remote", self.text)
        self.assertIn("git remote add origin", self.text)

    def test_it_builds_before_it_replaces_anything(self):
        """A failed build must not take the running dashboard down."""
        build = self.text.index("build")
        up = self.text.index("up -d --no-build --force-recreate")
        self.assertLess(build, up)

    def test_it_keeps_the_running_image(self):
        self.assertIn(":previous", self.text)

    def test_it_waits_for_the_health_check(self):
        self.assertIn("healthz", self.text)
        self.assertIn("State.Health", self.text)

    def test_it_rolls_back(self):
        self.assertIn("Rolled back", self.text)
        self.assertIn('docker tag "$PREVIOUS_IMAGE" "$IMAGE"', self.text)

    def test_it_never_touches_the_settings_file(self):
        """An update must not be able to lose the credentials."""
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("rm ") or " > " in stripped and "config" in stripped:
                self.fail(f"the update script writes to the settings: {stripped}")
        self.assertNotIn("rm -rf", self.text)

    def test_it_checks_the_settings_are_writable(self):
        """The most common first-run failure, caught before the swap."""
        self.assertIn("os.access", self.text)

    def test_it_is_executable(self):
        self.assertTrue(os.access(UPDATE, os.X_OK))


class TestTheSecretsStayOut(unittest.TestCase):
    """The things that must never be committed."""

    def test_the_settings_directory_is_gitignored(self):
        """Otherwise the mounted settings file could be committed by accident."""
        patterns = [line.strip() for line in read(GITIGNORE).splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        self.assertIn("config.json", patterns)
        self.assertIn("config/", patterns)

    def test_the_docker_files_hold_no_secrets(self):
        for path in (DOCKERFILE, DOCKERIGNORE, COMPOSE, UPDATE):
            text = read(path)
            self.assertNotRegex(text, r"(?i)(password|api[_-]?key)\s*[:=]\s*[\"']?[A-Za-z0-9]{8,}")
            # A key-shaped secret, of the kind the dashboard holds.
            self.assertNotRegex(text, r"sk_[a-z]+_[A-Za-z0-9]{10,}")
            self.assertNotRegex(text, r"(?i)(secret|token)\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}")


if __name__ == "__main__":
    unittest.main()
