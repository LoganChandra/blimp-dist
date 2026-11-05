"""Runs Hermes's original script-style suites (kernel_checks.py, crew_checks.py) under pytest,
each against a freshly applied mesh schema in the isolated test database."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import TEST_DSN

HERE = Path(__file__).resolve().parent


@pytest.mark.parametrize("script", ["kernel_checks.py"])  # crew_checks.py retired with agents/crew.py (phase 3 hosts replace it)
def test_script_suite(script, fresh_db):
    env = {**os.environ, "BLIMPG_DSN": TEST_DSN, "BLIMP_WORKTREE_ROOTS": "/home/user/blimp/worktrees"}
    r = subprocess.run([sys.executable, str(HERE / script)], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "ALL GREEN" in r.stdout
