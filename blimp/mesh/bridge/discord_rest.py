"""Discord REST transport for the Bridge (spec §18.1: one bot plus a webhook persona per agent).

- Cards, timelines and threads are posted by the Blimp Bridge bot (so Deck can edit them).
- Agent messages are posted through the channel's webhook with the agent's name and avatar,
  so the Boss sees "Duke" / "Hermes" / "Frank" without any agent holding a Discord token.
- Rate limits (429) and 5xx raise, so the outbox retries with backoff; nothing is marked
  delivered without the id Discord returns.

Config (never in git): BRIDGE_CONFIG=~/blimp/secrets/bridge.json
{
  "bot_token": "...",
  "channels": {"board": "<channel id>", "hq": "<id>", "approvals": "<id>", ...},
  "webhooks": {"board": "https://discord.com/api/webhooks/<id>/<token>", "hq": "..."},
  "personas": {"duke": {"name": "Duke", "avatar_url": "..."}, "hermes": {...}, "frank": {...}}
}
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

API = "https://discord.com/api/v10"
MAX_CONTENT = 2000  # Discord message limit


class DiscordError(Exception):
    pass


def _clip(content: str) -> str:
    return content if len(content) <= MAX_CONTENT else content[: MAX_CONTENT - 20].rstrip() + "\n… (truncated)"


class DiscordTransport:
    def __init__(self, bot_token: str, channels: dict, webhooks: dict, personas: dict, opener=urllib.request.urlopen):
        self.bot_token = bot_token
        self.channels = channels
        self.webhooks = webhooks
        self.personas = personas
        self._open = opener

    @classmethod
    def from_env(cls) -> "DiscordTransport":
        path = Path(os.environ.get("BRIDGE_CONFIG", os.path.expanduser("~/blimp/secrets/bridge.json")))
        cfg = json.loads(path.read_text())
        return cls(cfg["bot_token"], cfg["channels"], cfg.get("webhooks", {}), cfg.get("personas", {}))

    def _req(self, method: str, url: str, body: dict | None, bot: bool) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "User-Agent": "BlimpBridge (blimp.my, 2.0)"}
        if bot:
            headers["Authorization"] = f"Bot {self.bot_token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            raise DiscordError(f"{method} {url.split('?')[0].rsplit('/', 2)[-2:]} -> {e.code}: {detail}") from None

    def _channel(self, name: str) -> str:
        cid = self.channels.get(name)
        if not cid:
            raise DiscordError(f"no Discord channel configured for #{name}")
        return cid

    def create_thread(self, channel: str, title: str, content: str, persona: str, nonce: str | None = None) -> tuple[str, str]:
        cid = self._channel(channel)
        body = {"content": _clip(content), "allowed_mentions": {"parse": []}}
        if nonce:
            body.update({"nonce": nonce, "enforce_nonce": True})
        msg = self._req("POST", f"{API}/channels/{cid}/messages", body, bot=True)
        th = self._req("POST", f"{API}/channels/{cid}/messages/{msg['id']}/threads",
                       {"name": title[:100], "auto_archive_duration": 10080}, bot=True)
        return th["id"], msg["id"]

    def post(self, channel: str, thread_id: str | None, content: str, persona: str, nonce: str | None = None) -> str:
        body = {"content": _clip(content), "allowed_mentions": {"parse": []}}
        hook = self.webhooks.get(channel)
        if persona in self.personas and hook:
            p = self.personas[persona]
            body.update({"username": p.get("name", persona), "avatar_url": p.get("avatar_url")})
            q = f"?wait=true" + (f"&thread_id={thread_id}" if thread_id else "")
            return self._req("POST", hook + q, body, bot=False)["id"]
        if nonce:
            body.update({"nonce": nonce, "enforce_nonce": True})  # bot messages only; webhooks ignore it
        target = thread_id or self._channel(channel)
        return self._req("POST", f"{API}/channels/{target}/messages", body, bot=True)["id"]

    def edit(self, channel: str, thread_id: str | None, message_id: str, content: str, persona: str) -> None:
        # Cards and timelines are always bot-posted (persona "deck"), so the bot can edit them.
        # A card is the thread's starter message and lives in the parent channel.
        where = self._channel(channel) if message_id and channel == "board" and persona == "deck" and not self._is_timeline(content) else (thread_id or self._channel(channel))
        self._req("PATCH", f"{API}/channels/{where}/messages/{message_id}", {"content": _clip(content)}, bot=True)

    @staticmethod
    def _is_timeline(content: str) -> bool:
        return content.startswith("Timeline")
