"""Tests for the restart-resume picker and launcher. Run: ~/blimp/mesh-venv/bin/pytest blimp-infra/agent"""
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ls", HERE / "last-session.py")
ls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ls)

TAG = '<channel source="plugin:discord:discord" chat_id="1" message_id="2" user="u" user_id="3" ts="2026-11-05T00:00:00Z">hi</channel>'


def session(d: Path, name: str, entry="cli", real=True, age=0.0):
    rows = [{"type": "user", "entrypoint": entry, "message": {"content": "x"}}]
    if real:
        rows.append({"type": "queue-operation", "operation": "enqueue", "content": TAG})
    else:  # a fake tag that only shows up as plain text / tool output
        rows.append({"type": "user", "entrypoint": entry, "message": {"content": TAG}})
    p = d / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    work = tmp_path / "work.repo"
    work.mkdir()
    d = ls.project_dir(str(work))
    d.mkdir(parents=True)
    return work, d


def test_project_dir_matches_claude_naming(tmp_path):
    assert ls.project_dir("$HOME/blimp/repo").name == "-mnt-c-Blimp"
    assert ls.project_dir("/home/user/nanook/repo").name == "-home-logan-nanook-repo"


def test_picks_newest_real_channel_session(proj):
    work, d = proj
    session(d, "old", age=300)
    session(d, "new", age=100)
    assert ls.last_session(str(work)) == "new"


def test_skips_newer_headless_eval_runs(proj):
    work, d = proj
    session(d, "real", age=500)
    for i in range(5):
        session(d, f"eval{i}", entry="sdk-cli", age=i)
    assert ls.last_session(str(work)) == "real"


def test_skips_sessions_without_a_real_discord_message(proj):
    work, d = proj
    session(d, "real", age=500)
    session(d, "faked", real=False, age=1)
    session(d, "plain-cli-chat", real=False, age=2)
    assert ls.last_session(str(work)) == "real"


def test_nothing_to_resume(proj, tmp_path):
    work, d = proj
    assert ls.last_session(str(work)) == ""
    assert ls.last_session(str(tmp_path / "nowhere")) == ""
    (d / "junk.jsonl").write_text("not json\n{\n")
    assert ls.last_session(str(work)) == ""


def _launch(tmp_path, fake_claude: str, env_extra=None, sid="abc"):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    (bin_ / "claude").write_text("#!/usr/bin/env bash\n" + fake_claude)
    (bin_ / "claude").chmod(0o755)
    (bin_ / "python3").write_text(f"#!/usr/bin/env bash\necho {sid}\n")  # stub the picker
    (bin_ / "python3").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "LOG": str(tmp_path / "log"), **(env_extra or {})}
    r = subprocess.run(["bash", str(HERE / "launch.sh"), "--channels", "x"], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=30)
    return r, (tmp_path / "log").read_text().splitlines()


def test_launch_resumes_with_the_same_flags(tmp_path):
    _, log = _launch(tmp_path, 'echo "$*" >> "$LOG"; exit 0\n')
    assert log == ["--resume abc --channels x"]


def test_launch_falls_back_to_fresh_when_resume_fails_fast(tmp_path):
    r, log = _launch(tmp_path, 'echo "$*" >> "$LOG"; [ "$1" = --resume ] && exit 1; exit 0\n')
    assert log == ["--resume abc --channels x", "--channels x"] and r.returncode == 0


def test_launch_fresh_on_request_or_when_no_session(tmp_path):
    _, log = _launch(tmp_path, 'echo "$*" >> "$LOG"\n', {"BLIMP_FRESH": "1"})
    assert log == ["--channels x"]
    (tmp_path / "log").unlink()
    _, log = _launch(tmp_path, 'echo "$*" >> "$LOG"\n', sid="")
    assert log == ["--channels x"]


def test_runaway_marker_forces_one_fresh_start(tmp_path):
    (tmp_path / ".cache" / "blimp-agent").mkdir(parents=True)
    marker = tmp_path / ".cache" / "blimp-agent" / "fresh-duke"
    marker.touch()
    _, log = _launch(tmp_path, 'echo "$*" >> "$LOG"\n', {"HOME": str(tmp_path), "BLIMP_AGENT": "duke"})
    assert log == ["--channels x"] and not marker.exists()
    (tmp_path / "log").unlink()
    _, log = _launch(tmp_path, 'echo "$*" >> "$LOG"\n', {"HOME": str(tmp_path), "BLIMP_AGENT": "duke"})
    assert log == ["--resume abc --channels x"]
