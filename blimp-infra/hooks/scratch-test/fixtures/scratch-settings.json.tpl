{
  "_comment": "Scratch-project settings for /tmp/duke-hook-scratch — same wiring as settings-snippet.json but pointing at __HOOKS_DIR__. For an optional LIVE override test (the P1.2 'verify, don't assert' step): cd /tmp/duke-hook-scratch && DUKE_CHANNEL=discord claude, say anything, and watch the Stop hook block exactly once. Never install this outside the scratch dir.",
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "__HOOKS_DIR__/stop-log-guard.sh",
            "timeout": 15
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "__HOOKS_DIR__/pretooluse-prod-guard.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "__HOOKS_DIR__/promptsubmit-context.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
