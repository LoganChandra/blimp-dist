#!/usr/bin/env bash
# fetch_datasets.sh — download the real public memory benchmarks into ./data/.
# LOCOMO auto-downloads from GitHub raw. LongMemEval is large and gated behind a
# HuggingFace dataset repo; we try the common resolve URLs and, if they fail,
# print exact manual steps. Idempotent: skips files already present + valid.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="$HERE/data"
mkdir -p "$DATA"

ok_json() { python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$1" 2>/dev/null; }

fetch() { # url dest
  local url="$1" dest="$2"
  if [ -s "$dest" ] && ok_json "$dest"; then
    echo "  ok (cached): $(basename "$dest")"; return 0
  fi
  echo "  downloading $(basename "$dest") ..."
  if curl -fL --retry 3 -o "$dest.tmp" "$url" 2>/dev/null && ok_json "$dest.tmp"; then
    mv "$dest.tmp" "$dest"; echo "  saved: $(basename "$dest")"; return 0
  fi
  rm -f "$dest.tmp"; return 1
}

echo "== LOCOMO (10 conversations, ~200 QA) =="
fetch "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json" \
      "$DATA/locomo10.json" \
  || echo "  FAILED — get locomo10.json from https://github.com/snap-research/locomo (data/locomo10.json) into $DATA/"

echo "== LongMemEval (500 Q; the 's' variant is ~40 sessions/Q) =="
LME_OK=0
for base in \
  "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main" \
  "https://huggingface.co/datasets/LongMemEval/LongMemEval/resolve/main" ; do
  if fetch "$base/longmemeval_oracle.json" "$DATA/longmemeval_oracle.json"; then LME_OK=1; fi
  if fetch "$base/longmemeval_s.json" "$DATA/longmemeval_s.json"; then LME_OK=1; fi
  [ "$LME_OK" = 1 ] && break
done
if [ "$LME_OK" = 0 ]; then
  cat <<'EOF'
  LongMemEval auto-download failed (HF gating/URL drift). Manual steps:
    1. Open https://github.com/xiaowu0162/LongMemEval  (follow its data link,
       usually a HuggingFace dataset or Google Drive).
    2. Download longmemeval_oracle.json (small, recommended first) and/or
       longmemeval_s.json (large, the real haystack).
    3. Drop them in: blimp-infra/brain-eval/harness/data/
  LOCOMO alone is enough for a first real number — you can proceed without this.
EOF
fi

echo "done. datasets in $DATA:"
ls -la "$DATA" 2>/dev/null | grep -E "\.json" || echo "  (none yet)"
