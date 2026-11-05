"""Per-task sandbox (spec §10.6): agent-chosen and check commands run in a throwaway container,
never on the host.

  docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL
             --security-opt no-new-privileges --pids-limit 256 --memory 2g --cpus 2
             --user <uid>:<gid> -v <worktree>:/work -w /work <image> sh -c <cmd>

- No network by default (spec: egress allowlist; "none" is the safe start; per-contract egress
  is a later policy change, R4).
- Only the task worktree is mounted; no home directory, no secrets, no Docker socket.
- The container's root filesystem is read-only; the worktree is the only writable path.
- Wall-clock timeout; the container is killed and removed on timeout.
Zero LLM.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from typing import Any

DEFAULT_IMAGE = os.environ.get("BLIMP_SANDBOX_IMAGE", "python:3.12-slim")
TIMEOUT_S = int(os.environ.get("BLIMP_SANDBOX_TIMEOUT_S", "600"))
TAIL = 4000


class DockerSandbox:
    def __init__(self, image: str = DEFAULT_IMAGE, network: str = "none", memory: str = "2g", cpus: str = "2",
                 timeout_s: int = TIMEOUT_S, docker: str = "docker"):
        if network not in ("none",):
            raise ValueError("sandbox network other than 'none' needs an egress policy (R4)")
        self.image, self.network, self.memory, self.cpus, self.timeout_s = image, network, memory, cpus, timeout_s
        self.docker = shutil.which(docker) or docker

    def argv(self, cmd: str, workdir: str, name: str) -> list[str]:
        return [self.docker, "run", "--rm", "--name", name, "--network", self.network, "--read-only",
                "--tmpfs", "/tmp:rw,size=256m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "256", "--memory", self.memory, "--cpus", self.cpus,
                "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                "-v", f"{os.path.realpath(workdir)}:/work", "-w", "/work", self.image, "sh", "-c", cmd]

    def run(self, cmd: str, workdir: str, task_id: str | None = None) -> tuple[dict[str, Any], str, int]:
        if not cmd.strip():
            return {"error": "empty command"}, "refused: empty command", 1
        if not os.path.isdir(workdir):
            return {"error": "no worktree"}, f"refused: worktree {workdir} does not exist", 1
        name = f"blimp-{(task_id or 'x').lower()}-{uuid.uuid4().hex[:8]}"
        try:
            p = subprocess.run(self.argv(cmd, workdir, name), capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True)
            return {"timeout_s": self.timeout_s, "exit_code": 124}, f"timed out after {self.timeout_s}s (container killed)", 124
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        last = next((ln for ln in reversed(out.strip().splitlines()) if ln.strip()), "")
        return ({"exit_code": p.returncode, "stdout": p.stdout[-TAIL:], "stderr": p.stderr[-TAIL:]},
                f"exit {p.returncode}; last line: {last[:200]}", p.returncode)
