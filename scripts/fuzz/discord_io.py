"""Injection + observation over Discord REST and the c-lord API (Issue #377).

Synchronous urllib client (no extra deps, mirrors ``tests/e2e/conftest.py``).

Injection modes:
  * ``spawn``   — ``POST /api/spawn`` creates a *fresh thread per scenario*.
    This is the default: a channel webhook message is ignored by c-lord
    (``claude_chat.py`` only creates threads via slash command / spawn), so a
    fresh thread must go through the API.
  * ``webhook`` — post the scenario as a turn into one pre-attached thread
    (``FUZZ_TEST_THREAD_ID``) via the channel webhook, exercising multi-turn
    session continuity instead of fresh-thread isolation.

Observation polls the thread for the bot's final answer (the plain-content
message c-lord posts via ``POST /api/reply``), while collecting the *union* of
status reactions seen on the seed/trigger message so a transient ❌/⏳/⚠️ that the
lamp later overwrites is still captured.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .oracle import Observation
from .scenarios import Scenario

UA = "DiscordBot (https://github.com/yousan/c-lord, 1.0)"
DISCORD_API = "https://discord.com/api/v10"

# Discord hard limit for a single message. Scenarios longer than this cannot be
# sent at all (the seed ``thread.send`` would 400), so we truncate and note it —
# a >2000-char single message is not a realistic non-Nitro user input anyway.
DISCORD_MAX_CONTENT = 2000


class FuzzClient:
    """REST + c-lord-API client used by the fuzz runner."""

    def __init__(
        self,
        *,
        bot_token: str,
        api_url: str,
        api_secret: str | None = None,
        webhook_url: str | None = None,
        skip_health: bool = False,
    ) -> None:
        self._token = bot_token
        self._api_url = api_url.rstrip("/")
        self._api_secret = api_secret or None
        self._webhook = webhook_url or None
        # When the bot's REST API is not reachable from the harness host (e.g.
        # webhook-only injection, or a drifted CLORD_API_URL), skip the health
        # probe so it does not flag a spurious HEALTH_DOWN on every scenario.
        self._skip_health = skip_health

    # -- low-level ----------------------------------------------------------
    def _discord_get(self, path: str) -> Any:
        req = urllib.request.Request(f"{DISCORD_API}{path}")
        req.add_header("User-Agent", UA)
        req.add_header("Authorization", f"Bot {self._token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def _post(self, url: str, payload: dict, *, headers: dict[str, str]) -> tuple[int, Any]:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("User-Agent", UA)
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                parsed = json.loads(body) if body else None
            except ValueError:
                parsed = body.decode("utf-8", "replace")
            return exc.code, parsed

    # -- health -------------------------------------------------------------
    def health(self) -> bool:
        if self._skip_health:
            return True
        req = urllib.request.Request(f"{self._api_url}/api/health")
        req.add_header("User-Agent", UA)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    # -- injection ----------------------------------------------------------
    def spawn(self, prompt: str, channel_id: str) -> tuple[str | None, str | None]:
        """POST /api/spawn → (thread_id, None) on success, (None, error) on failure."""
        headers = {}
        if self._api_secret:
            headers["Authorization"] = f"Bearer {self._api_secret}"
        status, body = self._post(
            f"{self._api_url}/api/spawn",
            {"prompt": prompt, "channel_id": channel_id},
            headers=headers,
        )
        if status == 201 and isinstance(body, dict) and body.get("thread_id"):
            return str(body["thread_id"]), None
        return None, f"/api/spawn returned {status}: {body}"

    def webhook_post(self, content: str, thread_id: str) -> tuple[str | None, str | None]:
        if not self._webhook:
            return None, "no webhook configured (FUZZ_WEBHOOK_URL)"
        url = f"{self._webhook}?wait=true&thread_id={thread_id}"
        status, body = self._post(
            url,
            {"content": content, "username": "Fuzz Tester"},
            headers={},
        )
        if status in (200, 201) and isinstance(body, dict) and body.get("id"):
            return str(body["id"]), None
        return None, f"webhook POST returned {status}: {body}"

    def post_message(self, channel_id: str, content: str) -> tuple[bool, str | None]:
        """Post a plain message to a channel as the bot (used for the report)."""
        status, body = self._post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            {"content": content},
            headers={"Authorization": f"Bot {self._token}"},
        )
        if status in (200, 201):
            return True, None
        return False, f"POST messages returned {status}: {body}"

    # -- reads --------------------------------------------------------------
    def fetch_messages(self, channel_id: str, *, limit: int = 100) -> list[dict]:
        try:
            return self._discord_get(f"/channels/{channel_id}/messages?limit={limit}")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return []


def _reaction_names(message: dict) -> set[str]:
    names: set[str] = set()
    for r in message.get("reactions", []) or []:
        emoji = (r or {}).get("emoji") or {}
        name = emoji.get("name")
        if name:
            names.add(name)
    return names


def _find_seed(messages: list[dict], scenario_text: str, bot_id: str) -> dict | None:
    """The bot-authored message whose content matches the spawned prompt."""
    needle = scenario_text[:DISCORD_MAX_CONTENT].strip()
    for m in messages:
        if m.get("author", {}).get("id") != bot_id or m.get("webhook_id"):
            continue
        if (m.get("content") or "").strip() == needle:
            return m
    return None


def _collect_answer(
    messages: list[dict], *, bot_id: str, exclude_ids: set[str], scenario_text: str
) -> str | None:
    """Concatenate the bot's plain-content answer messages (oldest→newest).

    Filters out the seed, status/CLI-input lines (``-#`` prefix), webhook posts,
    and tool-use embeds (empty content). Returns None when no answer is present.
    """
    needle = scenario_text[:DISCORD_MAX_CONTENT].strip()
    chunks: list[tuple[int, str]] = []
    for m in messages:
        if m.get("author", {}).get("id") != bot_id or m.get("webhook_id"):
            continue
        if m.get("id") in exclude_ids:
            continue
        content = (m.get("content") or "").strip()
        if not content or content.startswith("-#"):
            continue
        if content == needle:  # the seed echoed back
            continue
        chunks.append((int(m["id"]), m.get("content") or ""))
    if not chunks:
        return None
    chunks.sort(key=lambda c: c[0])
    return "\n".join(c[1] for c in chunks)


def inject_and_observe(
    client: FuzzClient,
    scenario: Scenario,
    *,
    mode: str,
    channel_id: str,
    bot_id: str,
    webhook_thread_id: str | None = None,
    timeout: float = 180.0,
    poll: float = 4.0,
) -> Observation:
    """Inject one scenario and observe the bot's response. Pure-ish orchestration."""
    text = scenario.text
    if len(text) > DISCORD_MAX_CONTENT:
        text = text[: DISCORD_MAX_CONTENT - 1]

    started = time.monotonic()
    reactions_seen: set[str] = set()

    if mode == "webhook":
        observe_channel = webhook_thread_id or ""
        trigger_id, err = client.webhook_post(text, observe_channel)
        if trigger_id is None:
            return Observation(
                scenario.id,
                scenario.category,
                False,
                None,
                False,
                None,
                [],
                None,
                client.health(),
                err,
            )
        status_id = trigger_id
        exclude = {trigger_id}
    else:  # spawn
        thread_id, err = client.spawn(text, channel_id)
        if thread_id is None:
            return Observation(
                scenario.id,
                scenario.category,
                False,
                None,
                False,
                None,
                [],
                None,
                client.health(),
                err,
            )
        observe_channel = thread_id
        status_id = None  # discovered below (the seed message)
        exclude = set()

    deadline = started + timeout
    reply_text: str | None = None
    replied = False
    while time.monotonic() < deadline:
        messages = client.fetch_messages(observe_channel, limit=100)
        if mode == "spawn" and status_id is None:
            seed = _find_seed(messages, text, bot_id)
            if seed is not None:
                status_id = seed["id"]
                exclude = {status_id}
        if status_id is not None:
            for m in messages:
                if m.get("id") == status_id:
                    reactions_seen |= _reaction_names(m)
        answer = _collect_answer(messages, bot_id=bot_id, exclude_ids=exclude, scenario_text=text)
        if answer is not None:
            reply_text = answer
            replied = True
            break
        time.sleep(poll)

    # One last reaction sweep (a terminal ❌/🟡 may land right at the deadline).
    final = client.fetch_messages(observe_channel, limit=100)
    if status_id is not None:
        for m in final:
            if m.get("id") == status_id:
                reactions_seen |= _reaction_names(m)

    latency = (time.monotonic() - started) if replied else None
    return Observation(
        scenario.id,
        scenario.category,
        True,
        observe_channel,
        replied,
        reply_text,
        sorted(reactions_seen),
        latency,
        client.health(),
        None,
    )
