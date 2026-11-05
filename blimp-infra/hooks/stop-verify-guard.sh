#!/usr/bin/env bash
# stop-verify-guard.sh — god-tier upgrade #3, Stop hook (DRAFT — uninstalled).
#
# Deterministic enforcement of the verification doctrine's core rule
# ("work without a runnable check is drafted, not done"): if THIS turn
#   (a) made a world-changing mutation  (Edit / Write / MultiEdit /
#       mcp__blimp-brain__remember / task_update->done / a mutating Bash cmd),
#   (b) AND signs off as complete       (the final assistant message contains
#       completion language: done / fixed / shipped / deployed / verified / ...),
#   (c) AND shows NO evidence this turn  (no Task subagent / verifier / critic
#       spawn, no verify|critic|review Skill call, no test / build / check Bash),
# then block the stop ONCE (exit 2, stderr fed back to Claude) so the agent runs
# a real check — or spawns a verifier/critic — before finishing. Any one of the
# three not holding -> allow.
#
# Shared by BOTH Duke and Frank. Frank has no hooks harness yet; the companion
# settings-snippet-godtier.json registers this for him too. The channel gate
# reads a GENERIC agent-channel var (not DUKE_CHANNEL-only): it fires for any of
# AGENT_CHANNEL / NANOOK_CHANNEL / DUKE_CHANNEL = discord. NANOOK_CHANNEL is
# already exported by Frank's boot (boot-nanook.sh) and DUKE_CHANNEL by Duke's
# (boot-text-blimp.sh), so this works for BOTH with zero boot edits; AGENT_CHANNEL
# is the forward-looking generic name.
#
# Cannot wedge a headless / 3am session (the hard requirement — copied verbatim
# in spirit from stop-log-guard.sh):
#   - HOOK_DISABLE=1 kill-switch -> exit 0 (operator escape hatch);
#   - exits 0 unless an agent-channel var = discord — headless Dreams/eval/
#     overnight/`claude -p`/python runs export NONE of them, so they are never
#     gated;
#   - exits 0 when stop_hook_active=true (Claude is already continuing because of
#     a Stop hook — primary loop-prevention: at most ONE block per chain);
#   - a /tmp marker keyed by session+turn caps the block at ONCE per turn even if
#     stop_hook_active semantics ever shift;
#   - every parse/read failure fails OPEN (exit 0). `set -e` is deliberately
#     absent — a guard that crashes must never trap the agent.
#
# Stdin (hooks API): {"session_id", "transcript_path", "stop_hook_active", ...}
# Turn boundary: the last transcript line that is a REAL user prompt (type
# "user", not isMeta, content a string or an array with no tool_result blocks).
# Everything at/after it is "this turn".
set -uo pipefail

# --- Kill-switch: one env var disables the guard entirely. ------------------
[ "${HOOK_DISABLE:-}" = "1" ] && exit 0

# --- Channel gate: only enforce a live, human-attended Discord agent session.
# NANOOK_CHANNEL is exported by Frank's boot (boot-nanook.sh); DUKE_CHANNEL by
# Duke's (boot-text-blimp.sh); AGENT_CHANNEL is the generic forward-looking name.
# Headless runs export NONE of them -> exit 0 -> can never be Stop-hook-looped.
if [ "${AGENT_CHANNEL:-}" != "discord" ] \
   && [ "${NANOOK_CHANNEL:-}" != "discord" ] \
   && [ "${DUKE_CHANNEL:-}" != "discord" ]; then
  exit 0
fi

# No stdin (run by hand on a tty) or no jq -> nothing we can check; allow.
[ -t 0 ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

payload="$(cat 2>/dev/null)" || exit 0
[ -n "$payload" ] || exit 0

session="$(jq -r '.session_id // "unknown"' <<<"$payload" 2>/dev/null)" || exit 0
transcript="$(jq -r '.transcript_path // ""' <<<"$payload" 2>/dev/null)" || exit 0
active="$(jq -r '.stop_hook_active // false' <<<"$payload" 2>/dev/null)" || exit 0

# Already continuing because of a Stop hook -> never block twice.
[ "$active" = "true" ] && exit 0

# Transcript missing/unreadable -> fail open.
[ -n "$transcript" ] && [ -r "$transcript" ] || exit 0

# --- Signal lexicons (Oniguruma regex; matched case-insensitively). ---------
# A world-changing Bash command (append/write/commit/deploy/install/etc.).
# Single `>` is deliberately EXCLUDED (too common for read-only scratch files).
re_mut_bash='(git\s+(commit|push|merge|rebase|reset|tag|checkout\s+-b|cherry-pick|am\b)|(^|\s|;|&|\|)rm\s+-?|mv\s|sed\s+-i|tee\s|>>|mkdir\s|rmdir\s|chmod\s|chown\s|ln\s+-s|truncate\s|dd\s|systemctl\s+(--user\s+)?(enable|disable|start|stop|restart|reload|daemon-reload)|crontab\s|docker\s+(run|compose|rm|stop|start|restart|exec)|npm\s+publish|pip\s+install|apt(-get)?\s+install|cp\s|install\s+-|npx\s+\S+\s+deploy|vercel\b|supabase\s+db)'
# A check/test/build/inspection Bash command = evidence the agent verified.
re_chk_bash='(pytest|unittest|npm\s+(run\s+)?(test|build|lint|typecheck)|yarn\s+(test|build|lint)|pnpm\s+(test|build|lint)|go\s+test|cargo\s+(test|build|check|clippy)|bash\s+-n|sh\s+-n|zsh\s+-n|python3?\s+-c|py_compile|tsc\b|jest|vitest|playwright|cypress|eslint|ruff|mypy|shellcheck|jq\s+-n|node\s+-e|make\s+(test|check|lint)|ctest|rspec|phpunit|dotnet\s+test|curl\s|wget\s|\bhttp\s|git\s+(status|diff|log|show)|systemctl\s.*(status|is-active|is-enabled|list-timers|list-units)|journalctl|\bpsql\b|select\s|is-active|--dry-run|--help|-h\b)'
# A rigor Skill call (verifier / critic / reviewer) = evidence.
re_vfy_skill='(verify|verification|critic|peer.?review|green.?and.?clean|webshots|code.?review|security.?review|foundry-verification|foundry-critic)'
# Completion sign-off language in the FINAL assistant message.
re_claim='(\bdone\b|\bfixed\b|\bshipped\b|\bdeployed\b|\bcomplete(d)?\b|\bfinished\b|\bcommitted\b|\bpushed\b|\bmerged\b|\bresolved\b|\bverified\b|\bconfirmed\b|\btested\b|\bpatched\b|\bimplemented\b|works now|now working|working now|it works|is working|up and running|live now|all set|good to go|ready to (ship|go|deploy|merge))'

# One jq pass over the transcript tail. Emits:
#   <turn-id>\t<mutation-count>\t<claim-flag>\t<evidence-count>
result="$(tail -n 5000 -- "$transcript" 2>/dev/null | jq -Rrs \
  --arg mut "$re_mut_bash" --arg chk "$re_chk_bash" \
  --arg vfy "$re_vfy_skill" --arg clm "$re_claim" '
  split("\n")
  | map(fromjson? // empty)
  | map(select(type == "object"))
  | . as $lines
  | ([ $lines | to_entries[]
       | select(.value.type? == "user")
       | select((.value.isMeta? // false) | not)
       | select(
           ((.value.message.content? | type) == "string")
           or (((.value.message.content? | type) == "array")
               and (([ .value.message.content[]? | select(.type? == "tool_result") ] | length) == 0))
         )
       | .key
     ]) as $prompts
  | if ($prompts | length) == 0
    then "noturn\t0\t0\t0"
    else ($prompts | max) as $start
       | (($lines[$start].uuid? // ($start | tostring)) | tostring) as $turn
       # all tool_use blocks in this turn
       | ([ $lines[$start:][]
            | select(.type? == "assistant")
            | (.message.content? // []) | if type == "array" then .[] else empty end
            | select(.type? == "tool_use") ]) as $tools
       # (a) mutation count
       | ([ $tools[]
            | (.name? // "") as $n
            | ((.input? // {}) | tostring) as $in
            | select(
                ($n | test("(^|__)(Edit|Write|MultiEdit|NotebookEdit|remember)$"))
                or (($n | test("(^|__)task_update$")) and ($in | test("done|complete"; "i")))
                or (($n | test("(^|__)Bash$")) and ($in | test($mut; "i")))
              )
          ] | length) as $m
       # (c) evidence count
       | ([ $tools[]
            | (.name? // "") as $n
            | ((.input? // {}) | tostring) as $in
            | select(
                ($n | test("(^|__)Task$"))
                or (($n | test("(^|__)Skill$")) and ($in | test($vfy; "i")))
                or (($n | test("(^|__)Bash$")) and ($in | test($chk; "i")))
              )
          ] | length) as $e
       # (b) completion claim in the FINAL assistant message text
       | ([ $lines[$start:][] | select(.type? == "assistant") ] | last) as $la
       | ($la.message.content?) as $lc
       | (if ($lc | type) == "string" then $lc
          elif ($lc | type) == "array"
          then ([ $lc[] | select(.type? == "text") | (.text? // "") ] | join(" "))
          else "" end) as $ctext
       | (if ($ctext | test($clm; "i")) then 1 else 0 end) as $c
       | "\($turn)\t\($m)\t\($c)\t\($e)"
    end
' 2>/dev/null)" || exit 0
[ -n "$result" ] || exit 0

turn="$(printf '%s' "$result" | cut -f1)"
mut="$(printf '%s' "$result" | cut -f2)"
claim="$(printf '%s' "$result" | cut -f3)"
evic="$(printf '%s' "$result" | cut -f4)"

# Could not identify a turn -> fail open.
[ "$turn" = "noturn" ] && exit 0
# Non-numeric counts (parse glitch) -> fail open.
case "$mut$claim$evic" in '' | *[!0-9]*) exit 0 ;; esac

# Block only when ALL three hold: mutation AND claim AND no evidence.
[ "$mut"   -ge 1 ] || exit 0
[ "$claim" -ge 1 ] || exit 0
[ "$evic"  -eq 0 ] || exit 0

# Max once per turn: marker in /tmp keyed by session+turn.
key="$(printf '%s' "${session}:${turn}" | md5sum 2>/dev/null | cut -d' ' -f1)" || exit 0
[ -n "$key" ] || exit 0
marker="/tmp/.blimp-verify-guard-${key}"
[ -e "$marker" ] && exit 0
touch "$marker" 2>/dev/null || true
# Opportunistic cleanup of markers older than a day (cheap, best-effort).
find /tmp -maxdepth 1 -name '.blimp-verify-guard-*' -mmin +1440 -delete 2>/dev/null || true

echo "Stop blocked (once per turn): this turn changed state (Edit/Write/Bash/remember/task_update) and signs off as complete, but shows NO verification this turn — no Task subagent or verifier/critic spawn, no test/build/check command. Doctrine: work without a runnable check is drafted, not done. Run a real check that EXERCISES the change (or spawn a verifier/critic via the Task tool), report the evidence, then finish." >&2
exit 2
