#!/usr/bin/env bash
# upgrade-brain.sh <image> [env-overrides-file]: move the brain to a new Hindsight image (and/or env),
# with a full backup and an automatic rollback.
#
# The 2026-11-06 move (memory exam on a clone, same dump: 38/40 -> 39/40, recall@5 35 -> 36, no type
# worse, p50 recall 3.5 s -> 5.5 s):
#   bash upgrade-brain.sh ghcr.io/vectorize-io/hindsight@sha256:<1.2.0 digest> blimp-infra/postgres/brain-1.2.env
#
# How: stop the brain, copy its data dir (consistent: it's stopped), keep the old container as
# <name>-prev, start the new image on the same data/ports/env (+ overrides). The new image migrates the
# database on start, which the old one can't read, so a rollback puts the copied data dir back too.
# Rolls back by itself if the new one isn't healthy in 5 minutes or the memory count changed.
# Downtime: a few minutes. Agents' brain calls fail open meanwhile.
# Undo later by hand: bash upgrade-brain.sh --rollback
# BRAIN_CONTAINER (default duke-hindsight) lets it be rehearsed on a clone first.
set -euo pipefail
C="${BRAIN_CONTAINER:-duke-hindsight}"
STATE="$HOME/.cache/blimp-brain-upgrade/$C"; mkdir -p "$STATE"

port_args() { docker inspect "$1" --format '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}}-p {{.HostIp}}:{{.HostPort}}:{{$p}} {{end}}{{end}}' | sed 's#/tcp##g'; }
data_dir() { docker inspect "$1" --format '{{range .Mounts}}{{if eq .Destination "/home/hindsight/.pg0"}}{{.Source}}{{end}}{{end}}'; }
pgcount() { local b; b=$(docker exec "$1" sh -c 'ls -d /home/hindsight/.pg0/installation/*/bin | head -1')
  docker exec -e PGPASSWORD=hindsight "$1" "$b/psql" -h /tmp -p 5432 -U hindsight -d hindsight -tAc 'select count(*) from memory_units'; }
healthy() { local port="$1"; for i in $(seq 1 100); do curl -fs "127.0.0.1:$port/health" >/dev/null 2>&1 && return 0; sleep 3; done; return 1; }

rollback() {
  trap - ERR
  echo "Rolling back to the old container and data."
  local data bk; data=$(cat "$STATE/data"); bk=$(cat "$STATE/backup")
  [ -d "$bk" ] || { echo "Backup $bk is missing: NOT touching anything. Fix by hand."; exit 2; }
  if [ -f "$STATE/prev_id" ] && [ "$(docker inspect -f '{{.Id}}' "${C}-prev" 2>/dev/null)" != "$(cat "$STATE/prev_id")" ]; then
    echo "${C}-prev is not the container this upgrade kept (another script replaced it): NOT rolling back."; exit 2
  fi
  echo "Backup taken $(stat -c %y "$bk" | cut -c1-16); anything written since then is lost by this rollback."
  docker rm -f "$C" >/dev/null 2>&1 || true
  # Keep the failed data dir aside rather than deleting it, then put the backup back.
  sudo mv "$data" "$data.failed-$(date +%Y%m%d-%H%M%S)" || { echo "Couldn't move the data dir aside: old container NOT started."; exit 2; }
  sudo cp -a "$bk" "$data" || { echo "DATA RESTORE FAILED: old container NOT started. Backup is at $bk."; exit 2; }
  docker rename "${C}-prev" "$C" && docker update --restart unless-stopped "$C" >/dev/null && docker start "$C" >/dev/null
  healthy "$(cat "$STATE/port")" && echo "Back on the old image, healthy." || echo "OLD CONTAINER NOT HEALTHY: check docker logs $C"
  exit 1
}
[ "${1:-}" = "--rollback" ] && { docker inspect "${C}-prev" >/dev/null 2>&1 || { echo "No ${C}-prev to roll back to."; exit 1; }; rollback; }

IMAGE="${1:?usage: upgrade-brain.sh <image> [env-overrides-file] | --rollback}"
OVR="${2:-}"
DATA=$(data_dir "$C"); PORTS=$(port_args "$C")
PORT=$(docker inspect "$C" --format '{{range $p, $b := .HostConfig.PortBindings}}{{if eq $p "8888/tcp"}}{{range $b}}{{.HostPort}}{{end}}{{end}}{{end}}')
[ -n "$DATA" ] && [ -d "$DATA" ] && [ -n "$PORT" ] || { echo "Can't read $C's data dir or API port."; exit 1; }
COUNT_N=$(pgcount "$C")  # writes can land until the stop, so the new brain must have AT LEAST this many
ENVF="$STATE/env"; ( umask 077; docker inspect "$C" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(HINDSIGHT_|HF_)' > "$ENVF" )
if [ -n "$OVR" ]; then
  while IFS= read -r l || [ -n "$l" ]; do case "$l" in ''|'#'*) continue ;; esac; k="${l%%=*}"
    grep -v "^$k=" "$ENVF" > "$ENVF.t" || true; mv "$ENVF.t" "$ENVF"; echo "$l" >> "$ENVF"; done < "$OVR"
fi
chmod 600 "$ENVF"
echo "Brain $C: $COUNT_N memories, data $DATA, port $PORT -> $IMAGE"

docker pull -q "$IMAGE" >/dev/null 2>&1 || docker image inspect "$IMAGE" >/dev/null
sudo -n true 2>/dev/null || { echo "Needs passwordless sudo (for the data-dir copy). Run: sudo -v, then re-run."; exit 1; }
BK="$DATA.bak-upgrade-$(date +%Y%m%d-%H%M%S)"
[ -e "$BK" ] && { echo "$BK already exists"; exit 1; }
# Until the full rollback is armed, any failure just brings the old container back up as it was.
early() { trap - ERR; echo "Failed before the switch; restarting the old brain."; docker inspect "$C" >/dev/null 2>&1 || docker rename "${C}-prev" "$C"; docker update --restart unless-stopped "$C" >/dev/null; docker start "$C" >/dev/null; exit 1; }
trap early ERR
echo "Stopping $C (60 s grace for a clean database shutdown) and backing up its data dir..."
docker stop -t 60 "$C" >/dev/null
BEFORE_N="$COUNT_N"
sudo cp -a "$DATA" "$BK"
echo "$DATA" > "$STATE/data"; echo "$BK" > "$STATE/backup"; echo "$PORT" > "$STATE/port"
docker rm -f "${C}-prev" >/dev/null 2>&1 || true
docker rename "$C" "${C}-prev"; docker update --restart no "${C}-prev" >/dev/null
docker inspect -f '{{.Id}}' "${C}-prev" > "$STATE/prev_id"
trap rollback ERR
# shellcheck disable=SC2086
docker run -d --name "$C" --restart unless-stopped --shm-size 1g --env-file "$ENVF" $PORTS \
  -v "$DATA:/home/hindsight/.pg0" "$IMAGE" >/dev/null
healthy "$PORT"
[ -z "${UPGRADE_FORCE_FAIL:-}" ]   # rehearsal hook: prove the rollback path
AFTER=$(pgcount "$C")
[ "$AFTER" -ge "$BEFORE_N" ] || { echo "Memories went DOWN: $BEFORE_N -> $AFTER"; false; }
trap - ERR
echo "Brain $C is on $IMAGE with $AFTER memories (had $BEFORE_N before the stop)."
echo "Backup: $BK. Old container: ${C}-prev. Undo: bash $0 --rollback"
echo "Once you're happy: docker rm ${C}-prev && sudo rm -rf '$BK'"
