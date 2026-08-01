#!/usr/bin/env bash
set -euo pipefail

BASE="/opt/intel-news-bot"
LOCK="$BASE/data/pipeline.lock"

exec 9>"$LOCK"

/usr/bin/flock -w 180 9

"$BASE/venv/bin/python" "$BASE/app/collector.py"
"$BASE/venv/bin/python" "$BASE/app/classifier.py"
"$BASE/venv/bin/python" "$BASE/app/deduplicator.py"
