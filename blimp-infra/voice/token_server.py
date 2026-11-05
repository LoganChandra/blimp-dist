#!/usr/bin/env python3
"""
token_server.py — minimal LiveKit token minter + static front-end host.

Single-operator internal tool for the Blimp voice agent. It does two things:

  1. GET /            -> serves web/index.html (the mic UI)
  2. GET /token       -> mints a short-lived LiveKit access token for the
                         operator to join the voice room, and returns the
                         LiveKit ws URL so the browser never hardcodes it.

Everything is configured from the environment (see .env.example). No secrets
live in this file. This is INTERNAL infra, not a client product, so there is
no auth UI — bind it to localhost (or behind the droplet firewall) and only
the operator reaches it.

Run:
    uvicorn token_server:app --host 127.0.0.1 --port ${TOKEN_SERVER_PORT:-8790}
or simply:
    python token_server.py
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

# livekit-api server SDK. Verified against docs.livekit.io (May 2026):
#   from livekit.api import AccessToken, VideoGrants
#   AccessToken(api_key, api_secret)
#       .with_identity(...).with_name(...)
#       .with_grants(VideoGrants(room_join=True, room=...))
#       .with_ttl(datetime.timedelta(...))
#       .to_jwt()
from livekit.api import AccessToken, VideoGrants

# Load .env sitting next to this file (if present). Real deployments inject the
# environment via systemd EnvironmentFile, but loading here makes local dev work.
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

# --- Configuration (env only) ------------------------------------------------

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

# The room the agent worker also joins. Keep this in sync with agent.py — the
# agent is dispatched to whichever room the operator connects to, but using a
# stable name keeps the single-operator setup predictable.
VOICE_ROOM_NAME = os.getenv("VOICE_ROOM_NAME", "blimp-voice")

# How long a minted token is valid. The operator (re)fetches on each page load,
# so a short TTL is fine and limits replay if a token leaks.
TOKEN_TTL_MINUTES = int(os.getenv("TOKEN_TTL_MINUTES", "60"))

# Optional: a browser-facing ws URL distinct from the internal LIVEKIT_URL.
# On a droplet the agent talks to ws://localhost:7880 but the browser must hit
# the public host (e.g. wss://voice.example.com). If set, this is what the
# /token response hands the browser; otherwise we fall back to LIVEKIT_URL.
PUBLIC_LIVEKIT_URL = os.getenv("PUBLIC_LIVEKIT_URL", LIVEKIT_URL)

WEB_DIR = _HERE / "web"
INDEX_HTML = WEB_DIR / "index.html"

app = FastAPI(title="Blimp Voice — Token Server", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe — also surfaces whether creds are configured."""
    return JSONResponse(
        {
            "ok": True,
            "livekit_url": PUBLIC_LIVEKIT_URL,
            "room": VOICE_ROOM_NAME,
            "creds_configured": bool(LIVEKIT_API_KEY and LIVEKIT_API_SECRET),
        }
    )


@app.get("/token")
def mint_token(identity: str = "operator", name: str = "Operator") -> JSONResponse:
    """
    Mint a LiveKit access token for the operator to join VOICE_ROOM_NAME.

    Returns the token plus the ws URL and room so the front-end is fully
    self-configuring (it never hardcodes the LiveKit endpoint).
    """
    if not (LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
        raise HTTPException(
            status_code=500,
            detail="LIVEKIT_API_KEY / LIVEKIT_API_SECRET not configured in the environment.",
        )

    grants = VideoGrants(
        room_join=True,
        room=VOICE_ROOM_NAME,
        can_publish=True,      # publish the mic
        can_subscribe=True,    # hear the agent
        can_publish_data=True, # control/data messages if ever needed
    )

    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name)
        .with_grants(grants)
        .with_ttl(timedelta(minutes=TOKEN_TTL_MINUTES))
        .to_jwt()
    )

    return JSONResponse(
        {
            "token": token,
            "url": PUBLIC_LIVEKIT_URL,
            "room": VOICE_ROOM_NAME,
            "identity": identity,
        }
    )


@app.get("/")
def index() -> FileResponse:
    """Serve the single-page mic UI."""
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail="web/index.html not found")
    return FileResponse(INDEX_HTML, media_type="text/html")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("TOKEN_SERVER_PORT", "8790"))
    # Bind to localhost by default — this is single-operator internal infra.
    # Public exposure (rail: 0.0.0.0 bind) is an explicit, deliberate choice;
    # set TOKEN_SERVER_HOST=0.0.0.0 only behind a firewall / reverse proxy.
    host = os.getenv("TOKEN_SERVER_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
