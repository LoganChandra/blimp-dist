"""
DEPRECATED. This script's job (ingesting repo docs into a custom memories table)
was replaced by Hindsight, which handles memory natively. Use:

    duke-mem ingest-repo

See hindsight_client.py for the new implementation, and the README for the
architecture rationale.

This shim remains so older docs / commands don't 404; it forwards to the new path.
"""

import os
import sys

if __name__ == "__main__":
    print("[seed_repo.py is deprecated — forwarding to: duke-mem ingest-repo]", file=sys.stderr)
    os.execvp("duke-mem", ["duke-mem", "ingest-repo"] + sys.argv[1:])
