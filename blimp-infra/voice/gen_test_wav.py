#!/usr/bin/env python3
"""gen_test_wav.py — synthesize a short speech WAV for browser fake-mic capture.

Writes test_speech.wav (16-bit PCM) containing a spoken utterance + trailing
silence so the agent's VAD sees an end-of-turn. Used by run-browser-test.sh via
chromium's --use-file-for-fake-audio-capture, so the in-browser test exercises
the real STT→LLM→TTS round trip, not just the greeting.
"""
import asyncio, os, wave
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
from livekit.plugins import cartesia

KEY = os.environ["CARTESIA_API_KEY"]
MODEL = os.environ.get("CARTESIA_TTS_MODEL", "sonic-2")
VOICE = os.environ.get("TTS_VOICE_ID") or os.environ.get(
    "DUKE_TTS_VOICE_ID", "4df027cb-2920-4a1f-8c34-f21529d5c3fe")
OUT = Path(__file__).resolve().parent / "test_speech.wav"
TEXT = "Hey Duke, can you hear me? Please say hello back."


async def main() -> int:
    import aiohttp
    http = aiohttp.ClientSession()
    try:
        tts = cartesia.TTS(model=MODEL, voice=VOICE, api_key=KEY, http_session=http)
        frames = [ev.frame async for ev in tts.synthesize(TEXT)]
        if not frames:
            print("no frames"); return 1
        sr = frames[0].sample_rate
        ch = frames[0].num_channels
        data = b"".join(bytes(f.data) for f in frames)
        data += b"\x00" * (sr * ch * 2 * 2)   # +2s silence so VAD ends the turn
        with wave.open(str(OUT), "wb") as w:
            w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(data)
        print(f"wrote {OUT} ({len(data)} bytes, {sr}Hz x{ch})")
        return 0
    finally:
        await http.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
