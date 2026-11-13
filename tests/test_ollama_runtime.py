"""P1-5: the bundled Ollama runs unprivileged, limited and unpublished, and the
shared-Ollama path the Pi uses is unchanged.

These read docker-compose.yml as text, like the other compose tests; they do
not start anything. The runtime checks are in the roadmap's P1-5 entry.
"""

import os
import re
import unittest
from unittest.mock import patch

from newsbot import config as nb_config
from tests.test_holding_horizon import compose_value

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED = ("ollama-volume-init", "ollama", "ollama-pull")
UID = "10002"


def read(*path):
    with open(os.path.join(ROOT, *path), encoding="utf-8") as handle:
        return handle.read()


def section(name):
    """A top-level section of docker-compose.yml, e.g. `services`."""
    match = re.search(rf"^{name}:\n(.*?)(?=^\S|\Z)", read("docker-compose.yml"), re.M | re.S)
    return match.group(1)


def service(name):
    """One service's block: its lines up to the next two-space-indented line."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", section("services"), re.M | re.S)
    if not match:
        raise AssertionError(f"no service {name} in docker-compose.yml")
    return match.group(1)


def value(block, key):
    match = re.search(rf"^    {key}: (.+)$", block, re.M)
    return match.group(1).strip() if match else None


def listed(block, key):
    """The `- item` entries under `key:`, comments dropped."""
    match = re.search(rf"^    {key}:\n((?:      [-#].*\n)*)", block, re.M)
    if not match:
        return []
    return [re.sub(r"\s+#.*", "", line.strip()[2:]).strip()
            for line in match.group(1).splitlines() if line.strip().startswith("- ")]


class BundledOllamaTests(unittest.TestCase):
    def test_only_the_profile_starts_it_and_the_bot_never_waits_for_it(self):
        for name in BUNDLED:
            self.assertEqual(value(service(name), "profiles"), '["bundled-ollama"]', name)
        for name in ("news-notifier", "dashboard-streamlit"):
            self.assertIsNone(value(service(name), "profiles"), name)
        # Ranking falls back to the heuristic, so Ollama must never gate the bot.
        self.assertNotIn("depends_on", service("news-notifier"))

    def test_nothing_is_published_on_the_host(self):
        for name in BUNDLED:
            block = service(name)
            for key in ("ports:", "expose:", "network_mode: host", "privileged"):
                self.assertNotIn(key, block, name)

    def test_its_network_is_shared_with_the_bot_and_no_one_else(self):
        self.assertEqual(listed(service("ollama"), "networks"), ["bundled-ollama"])
        self.assertEqual(listed(service("ollama-pull"), "networks"), ["bundled-ollama"])
        self.assertEqual(value(service("ollama-volume-init"), "network_mode"), "none")
        self.assertEqual(listed(service("news-notifier"), "networks"), ["default", "ranker", "bundled-ollama"])
        self.assertNotIn("bundled-ollama", service("dashboard-streamlit"))
        networks = section("networks")
        bundled = re.search(r"^  bundled-ollama:\n((?:    .*\n)*)", networks, re.M)
        self.assertIsNotNone(bundled)
        # Project-local: never `external`, so it cannot shadow a shared `ollama`.
        self.assertNotIn("external", bundled.group(1))

    def test_server_and_pull_run_as_the_uid_the_volume_is_handed_to(self):
        init, server, pull = (service(name) for name in BUNDLED)
        self.assertEqual(value(server, "user"), f'"{UID}:{UID}"')
        self.assertEqual(value(pull, "user"), f'"{UID}:{UID}"')
        entrypoint = re.sub(r"\s+", " ", init[init.index("entrypoint:"):init.index("volumes:")])
        self.assertIn(f'"!", "-user", "{UID}"', entrypoint)
        self.assertIn(f'"chown", "{UID}:{UID}"', entrypoint)
        self.assertEqual(listed(server, "environment"), ["HOME=/home/ollama"])
        # The volume is $HOME/.ollama in both, so the init chowns what the server uses.
        for block in (init, server):
            self.assertEqual(listed(block, "volumes"), ["ollama_models:/home/ollama/.ollama"])
        # The client asks the server over HTTP; it gets no model volume.
        self.assertEqual(listed(pull, "volumes"), [])
        for name in BUNDLED:
            self.assertNotIn("/root", service(name), name)
        # The server waits for the hand-over.
        self.assertIn("ollama-volume-init:\n        condition: service_completed_successfully", server)

    def test_no_capability_beyond_chown_for_the_one_shot(self):
        init, server, pull = (service(name) for name in BUNDLED)
        for name, block in zip(BUNDLED, (init, server, pull), strict=True):
            self.assertEqual(listed(block, "cap_drop"), ["ALL"], name)
            self.assertEqual(listed(block, "security_opt"), ["no-new-privileges:true"], name)
            self.assertEqual(value(block, "read_only"), "true", name)
        self.assertEqual(listed(init, "cap_add"), ["CHOWN"])
        self.assertEqual(listed(server, "cap_add"), [])
        self.assertEqual(listed(pull, "cap_add"), [])
        self.assertEqual(listed(server, "tmpfs"), ["/tmp"])

    def test_limits_are_configurable_and_match_env_example(self):
        server = service("ollama")
        self.assertEqual(value(server, "mem_limit"), "${BUNDLED_OLLAMA_MEM_LIMIT:-4g}")
        self.assertEqual(value(server, "cpus"), "${BUNDLED_OLLAMA_CPUS:-0}")
        example = read(".env.example")
        self.assertIn("\nBUNDLED_OLLAMA_MEM_LIMIT=4g\n", example)
        self.assertIn("\nBUNDLED_OLLAMA_CPUS=0\n", example)

    def test_restart_and_health_are_kept(self):
        server = service("ollama")
        self.assertEqual(value(server, "restart"), "unless-stopped")
        self.assertIn('test: ["CMD-SHELL", "ollama list >/dev/null 2>&1 || exit 1"]', server)
        for name in ("ollama-volume-init", "ollama-pull"):
            self.assertEqual(value(service(name), "restart"), '"no"', name)

    def test_one_image_pinned_by_digest(self):
        images = {value(service(name), "image") for name in BUNDLED}
        self.assertEqual(len(images), 1)
        self.assertRegex(images.pop(), r"^ollama/ollama:[\d.]+@sha256:[0-9a-f]{64}$")


class SharedOllamaTests(unittest.TestCase):
    """The Pi reaches another project's `ollama` over RANKER_NETWORK."""

    def test_the_ranker_network_still_follows_the_env(self):
        self.assertIn("  ranker:\n    name: ${RANKER_NETWORK:-daily-news-bot_ranker}\n"
                      "    external: ${RANKER_NETWORK_EXTERNAL:-false}\n", section("networks"))
        self.assertIn("ranker", listed(service("news-notifier"), "networks"))

    def test_compose_passes_the_endpoint_through(self):
        self.assertEqual(compose_value("NEWS_RANKER_URL", {}), "http://ollama:11434")
        self.assertEqual(compose_value("NEWS_RANKER_URL", {"NEWS_RANKER_URL": "http://gpu-box:11434"}),
                         "http://gpu-box:11434")

    def test_the_bot_reads_any_endpoint_from_the_environment(self):
        with patch.dict(os.environ, {"NEWS_RANKER_URL": "http://192.0.2.10:11434"}):
            self.assertEqual(nb_config.AppSettings(_env_file=None).news_ranker_url, "http://192.0.2.10:11434")
        with patch.dict(os.environ):
            os.environ.pop("NEWS_RANKER_URL", None)
            self.assertEqual(nb_config.AppSettings(_env_file=None).news_ranker_url, "http://localhost:11434")


if __name__ == "__main__":
    unittest.main()
