"""Per-task sandbox (spec §10.6), against the real Docker daemon (skipped if unavailable)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from blimp.mesh.toolgate.sandbox import DockerSandbox

IMAGE = "python:3.12-slim"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason=f"docker or image {IMAGE} unavailable")


@pytest.fixture()
def sb():
    return DockerSandbox(IMAGE, timeout_s=20)


def test_runs_in_the_worktree(sb, tmp_path):
    (tmp_path / "flag").write_text("green")
    res, summary, code = sb.run("cat flag && echo written > out.txt", str(tmp_path))
    assert code == 0 and "green" in res["stdout"] and (tmp_path / "out.txt").read_text().strip() == "written"


def test_no_network(sb, tmp_path):
    res, _, code = sb.run("python -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=3)\"", str(tmp_path))
    assert code != 0


def test_host_is_invisible(sb, tmp_path):
    res, _, code = sb.run("ls /home; test -e /var/run/docker.sock && echo SOCKET; cat /etc/hostname", str(tmp_path))
    assert "logan" not in res["stdout"] and "SOCKET" not in res["stdout"]


def test_root_filesystem_is_read_only(sb, tmp_path):
    res, _, code = sb.run("touch /usr/evil", str(tmp_path))
    assert code != 0


def test_timeout_kills_the_container(tmp_path):
    sb = DockerSandbox(IMAGE, timeout_s=3)
    res, summary, code = sb.run("sleep 30", str(tmp_path), task_id="T-0001")
    assert code == 124 and "killed" in summary
    left = subprocess.run(["docker", "ps", "-q", "--filter", "name=blimp-t-0001"], capture_output=True, text=True).stdout.strip()
    assert left == ""


def test_network_other_than_none_needs_policy():
    with pytest.raises(ValueError):
        DockerSandbox(IMAGE, network="bridge")


def test_check_runner_uses_the_sandbox(fresh_db, tmp_path):
    from blimp.mesh.deck.board import Board
    from blimp.mesh.toolgate.checks import CheckRunner

    from .test_board import contract
    b = Board(fresh_db)
    b.record_mandate("x", "t")
    (tmp_path / "flag").write_text("green")
    t = b.create_task(title="t", type="build", created_by="hermes", mandate_id="M-0001")
    from .conftest import mkwt
    wt = mkwt(t["id"])
    (tmp_path / "wt" / t["id"] / "flag").unlink()
    (tmp_path / "wt" / t["id"] / "flag").write_text("green")  # a real file: the sandbox cannot follow a symlink out
    b.propose(t["id"], contract(t["id"], worktree=wt,
                                acceptance_checks=[{"id": "c1", "kind": "cmd", "cmd": "cat flag; ls /home", "expected": "contains: green"}]), "hermes")
    b.approve_checks(t["id"], "logan")
    b.make_ready(t["id"], "duke")
    res, _, code = CheckRunner(fresh_db, sandbox=DockerSandbox(IMAGE, timeout_s=30)).run(t["id"], "c1")
    assert code == 0 and res["sandbox"] is True and "logan" not in res["output_tail"]
