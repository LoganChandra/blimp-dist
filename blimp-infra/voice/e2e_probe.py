#!/usr/bin/env python3
"""e2e_probe.py — synthetic-participant end-to-end test for the Blimp voice agent.

Proves the full round-trip WITHOUT a human mic:
  1. Synthesize a test utterance with Cartesia ("Hey Duke, can you hear me?...").
  2. Join room "blimp-voice" as a second participant.
  3. Publish that utterance as our mic track.
  4. Subscribe to the agent's audio track and measure audio received back.
  5. Capture {type:"agent_speaking"} data messages too.

Prints "E2E_RESULT=PASS" (exit 0) if the agent spoke back, else FAIL + diagnostics.
Run while agent.py is already LIVE in the same room. Named e2e_probe.py (NOT
*agent.py) so the agent's _kill_stale_agents() regex won't target it.
"""
from __future__ import annotations
import asyncio, os, time, uuid, sys, json
import aiohttp
from datetime import timedelta
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

from livekit import rtc
from livekit.api import AccessToken, VideoGrants
from livekit.plugins import cartesia

URL = os.environ["LIVEKIT_URL"]
KEY = os.environ["LIVEKIT_API_KEY"]
SECRET = os.environ["LIVEKIT_API_SECRET"]
ROOM = os.environ.get("VOICE_ROOM_NAME", "blimp-voice")
CART_KEY = os.environ["CARTESIA_API_KEY"]
CART_MODEL = os.environ.get("CARTESIA_TTS_MODEL", "sonic-2")
VOICE = os.environ.get("TTS_VOICE_ID", "62ae83ad-4f6a-430b-af41-a9bede9286ca")

UTTERANCE = "Hey Duke, can you hear me? Please say hello back."
LISTEN_SECONDS = 25
RESPONSE_BYTES_THRESHOLD = 20000   # ~0.2s of 24k mono int16 = a real reply


def log(m): print(f"[e2e {time.strftime('%H:%M:%S')}] {m}", flush=True)


async def synth_utterance(http) -> list:
    log(f"Synthesizing utterance via Cartesia ({CART_MODEL}, voice {VOICE[:8]}…)")
    tts = cartesia.TTS(model=CART_MODEL, voice=VOICE, api_key=CART_KEY, http_session=http)
    frames = []
    async for ev in tts.synthesize(UTTERANCE):
        frames.append(ev.frame)
    if frames:
        total = sum(len(bytes(f.data)) for f in frames)
        log(f"  {len(frames)} frames, {total} bytes, "
            f"{frames[0].sample_rate}Hz x{frames[0].num_channels}ch")
    else:
        log("  NO FRAMES")
    return frames


def mint_token(identity: str) -> str:
    return (
        AccessToken(KEY, SECRET)
        .with_identity(identity)
        .with_name("e2e-operator")
        .with_grants(VideoGrants(room_join=True, room=ROOM,
                                 can_publish=True, can_subscribe=True, can_publish_data=True))
        .with_ttl(timedelta(minutes=10))
        .to_jwt()
    )


async def main() -> int:
    http = aiohttp.ClientSession()
    rx = {"bytes": 0, "frames": 0, "first_t": None}
    data_msgs = []
    agent_tracks = []

    frames = await synth_utterance(http)
    if not frames:
        await http.close()
        print("E2E_RESULT=FAIL reason=no_test_audio")
        return 2

    room = rtc.Room()

    @room.on("track_subscribed")
    def _on_track(track, pub, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            log(f"Subscribed to agent audio from '{participant.identity}'")
            agent_tracks.append(track)
            astream = rtc.AudioStream(track)

            async def _drain():
                async for ev in astream:
                    f = ev.frame
                    rx["bytes"] += len(bytes(f.data))
                    rx["frames"] += 1
                    if rx["first_t"] is None and rx["bytes"] > 2000:
                        rx["first_t"] = time.time()
                        log("First agent audio bytes received.")
            asyncio.create_task(_drain())

    @room.on("data_received")
    def _on_data(pkt: rtc.DataPacket):
        try:
            m = json.loads(bytes(pkt.data).decode())
            data_msgs.append(m)
            log(f"data msg: {m}")
        except Exception:
            pass

    identity = f"e2e-operator-{uuid.uuid4().hex[:6]}"
    log(f"Connecting as {identity} to '{ROOM}' …")
    await room.connect(URL, mint_token(identity), options=rtc.RoomOptions(auto_subscribe=True))
    log(f"Connected. Remote participants present: "
        f"{[p.identity for p in room.remote_participants.values()]}")

    src = rtc.AudioSource(frames[0].sample_rate, frames[0].num_channels)
    track = rtc.LocalAudioTrack.create_audio_track("e2e-mic", src)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    log("Published our mic track.")

    # Give the agent a moment to (a) see + subscribe to us and (b) deliver its
    # greeting. Any audio received in this window before we speak = the greeting.
    await asyncio.sleep(4.0)
    greeting_bytes = rx["bytes"]
    log(f"Greeting audio received before we spoke: {greeting_bytes} bytes")

    speak_start = time.time()
    log("Speaking utterance into the room…")
    for f in frames:
        await src.capture_frame(f)
    spoke_done = time.time()
    log(f"Finished speaking ({round(spoke_done-speak_start,1)}s). Waiting for reply…")

    deadline = spoke_done + LISTEN_SECONDS
    bytes_at_speak_end = rx["bytes"]
    while time.time() < deadline:
        if rx["bytes"] - bytes_at_speak_end > RESPONSE_BYTES_THRESHOLD:
            break
        await asyncio.sleep(0.4)

    reply_bytes = rx["bytes"] - bytes_at_speak_end
    latency = (rx["first_t"] - spoke_done) if (rx["first_t"] and rx["first_t"] > spoke_done) else None

    log("─" * 50)
    log(f"agent audio tracks seen : {len(agent_tracks)}")
    log(f"total agent audio bytes : {rx['bytes']} ({rx['frames']} frames)")
    log(f"reply bytes after speak : {reply_bytes}")
    log(f"reply latency (s)       : {round(latency,2) if latency else 'n/a'}")
    log(f"data messages           : {len(data_msgs)} -> {data_msgs[:4]}")

    try:
        await room.disconnect()
    except Exception:
        pass
    await http.close()

    passed = len(agent_tracks) > 0 and reply_bytes > RESPONSE_BYTES_THRESHOLD
    greeted = greeting_bytes > RESPONSE_BYTES_THRESHOLD
    speaking_evt = any(m.get("type") == "agent_speaking" for m in data_msgs)
    print(f"E2E_RESULT={'PASS' if passed else 'FAIL'} "
          f"agent_tracks={len(agent_tracks)} reply_bytes={reply_bytes} "
          f"greeting_bytes={greeting_bytes} greeted={greeted} "
          f"speaking_events={speaking_evt} data_msgs={len(data_msgs)} "
          f"latency={round(latency,2) if latency else 'na'}")
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"E2E_RESULT=FAIL reason=exception:{type(e).__name__}:{str(e)[:120]}")
        sys.exit(3)
