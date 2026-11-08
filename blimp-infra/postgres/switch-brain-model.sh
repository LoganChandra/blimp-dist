#!/usr/bin/env bash
# switch-brain-model.sh <openrouter-model-id>: change the model Hindsight uses to turn chats into memories.
#
# The model is a container env var, so this recreates duke-hindsight with the same image, ports, volume,
# restart policy and env, only the model changed. Memories live in the mounted volume and are untouched.
# Safety: the data dir is copied first (container stopped, so the copy is consistent), the old container
# is kept as duke-hindsight-prev, and if the new one isn't healthy in 3 minutes it rolls back by itself.
# Downtime: about a minute. Agents' brain calls fail open meanwhile.
# Undo later: bash switch-brain-model.sh <old-model>   (or: docker rm -f duke-hindsight && docker rename duke-hindsight-prev duke-hindsight && docker start duke-hindsight)
set -euo pipefail
NEW="${1:?usage: switch-brain-model.sh <openrouter-model-id>}"
C=duke-hindsight
OLD_MODEL=$(docker inspect $C --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^HINDSIGHT_API_LLM_MODEL=//p')
[ "$OLD_MODEL" = "$NEW" ] && { echo "Already on $NEW."; exit 0; }
DATA=$(docker inspect $C --format '{{range .Mounts}}{{if eq .Destination "/home/hindsight/.pg0"}}{{.Source}}{{end}}{{end}}')
IMAGE=$(docker inspect $C --format '{{.Image}}')   # exact image id: never let a recreate jump to a newer :latest
[ -n "$DATA" ] && [ -d "$DATA" ] || { echo "Can't find the brain's data dir."; exit 1; }
ENVF=$(mktemp); chmod 600 "$ENVF"; trap 'rm -f "$ENVF"' EXIT
# Only the HINDSIGHT_*/HF_* settings; the image supplies PATH, PYTHON_* and the rest itself.
docker inspect $C --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(HINDSIGHT_|HF_)' \
  | grep -v '^HINDSIGHT_API_LLM_MODEL=' > "$ENVF"
echo "HINDSIGHT_API_LLM_MODEL=$NEW" >> "$ENVF"

echo "Stopping the brain and backing up $DATA ..."
docker stop $C >/dev/null
BK="$DATA.bak-$(date +%Y%m%d-%H%M)"
sudo cp -a "$DATA" "$BK"
docker rm -f ${C}-prev >/dev/null 2>&1 || true
docker rename $C ${C}-prev
docker update --restart no ${C}-prev >/dev/null

rollback() {
  echo "New container isn't healthy, rolling back to $OLD_MODEL."
  docker logs --tail 20 $C 2>&1 || true
  docker rm -f $C >/dev/null 2>&1 || true
  docker rename ${C}-prev $C && docker update --restart unless-stopped $C >/dev/null && docker start $C >/dev/null
  exit 1
}
docker run -d --name $C --restart unless-stopped --env-file "$ENVF" \
  -p 127.0.0.1:8888:8888 -p 127.0.0.1:9999:9999 -v "$DATA:/home/hindsight/.pg0" "$IMAGE" >/dev/null || rollback
for i in $(seq 1 60); do curl -fs 127.0.0.1:8888/health >/dev/null 2>&1 && break; sleep 3; done
curl -fs 127.0.0.1:8888/health >/dev/null 2>&1 || rollback
MEM=$(curl -fs 127.0.0.1:8888/v1/default/banks/duke/stats | python3 -c 'import json,sys;print(json.load(sys.stdin)["total_nodes"])') || rollback
sed -i "s|^HINDSIGHT_API_LLM_MODEL=.*|HINDSIGHT_API_LLM_MODEL=$NEW|; s|^HINDSIGHT_LLM_MODEL=.*|HINDSIGHT_LLM_MODEL=$NEW|" "$HOME/blimp/.env"
echo "Brain is on $NEW ($MEM memories, all there). Was: $OLD_MODEL."
echo "Backup: $BK. Old container kept as ${C}-prev; remove both once you're happy:"
echo "  docker rm ${C}-prev && sudo rm -rf '$BK'"
