#!/bin/bash
cd "$(dirname "$0")"
exec ./venv/bin/python -u agent.py > /tmp/agent.log 2>&1
