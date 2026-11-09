#!/usr/bin/env bash
# Gate 4 (capacity justification) — measure/collect.
# Usage: bash g4_capacity_sweep.sh {measure|collect}
#
# Preconditions:
#   1. GDS arm serving on PORT after `bash g2_context_sweep.sh setup` passed gate 1.
#   2. IMPORTANT — bring that arm up with a cuFile buffer (CUFILE_BUF_MIB) no larger
#      than ONE entry. Gate 4 reads a zero device-read as "resident in L1 (DRAM)".
#      If the cuFile buffer can hold several entries, NVMe-evicted entries are served
#      from a GPU-resident buffer and also read zero -- a phantom tier (Gate 1), which
#      would be misread as DRAM. Size L1 (LMCACHE_MAX_LOCAL_CPU_SIZE) to the DRAM
#      capacity whose overflow you want to find.
# This script does not start containers; it drives g4_capacity_sweep.py, then collects
# checksummed evidence.
set -uo pipefail

PORT=${PORT:-8102}          # GDS arm (g2_context_sweep.sh CTNR)
RECO_PORT=${RECO_PORT:-8100} # recompute arm (for the fast/slow read-0 reference)
# The client next to this script, not a fixed path in /tmp another user could write first.
CLIENT=${CLIENT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/g4_capacity_sweep.py}
ENTRY_TOKENS=${ENTRY_TOKENS:-4096}
WORKING_SETS=${WORKING_SETS:-1 2 4 8 16 24 32}
# A fresh EVIDENCE per run keeps an older run's files out of this one.
EV=${EVIDENCE:-/tmp/capacity-evidence}
# Evidence lands in a directory this operator owns; a pre-placed symlink would redirect it.
prepare_evidence_dir() {
  local dir=$1
  [[ ! -L "$dir" ]] || { echo "!! evidence path is a symlink: $dir" >&2; return 1; }
  mkdir -p "$dir"
  [[ -O "$dir" ]] || { echo "!! evidence path is not owned by $(id -un): $dir" >&2; return 1; }
}
prepare_evidence_dir "$EV" || exit 1

case "${1:-}" in

measure)
  sudo -v || exit 1
  [ -f "$CLIENT" ] || { echo "!! client not found: $CLIENT (set CLIENT)"; exit 1; }
  curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null \
    || { echo "!! GDS arm not responding on $PORT (run: bash g2_context_sweep.sh setup)"; exit 1; }
  curl -sf "http://127.0.0.1:$RECO_PORT/v1/models" >/dev/null \
    || { echo "!! recompute arm not responding on $RECO_PORT (needed for the read-0 reference)"; exit 1; }
  # shellcheck disable=SC2086
  python3 "$CLIENT" --port "$PORT" --recompute-port "$RECO_PORT" --entry-tokens "$ENTRY_TOKENS" \
    --working-sets $WORKING_SETS \
    --output "$EV/capacity.json" 2>&1 | tee "$EV/measure-console.txt"
  ;;

collect)
  cp "$CLIENT" "$EV"/ 2>/dev/null
  { date -u; uname -a; nvidia-smi --query-gpu=index,name,pci.bus_id --format=csv; } > "$EV/provenance.txt"
  ( cd "$EV" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | sort | xargs sha256sum > SHA256SUMS )
  tar czf "$HOME/$(basename "$EV").tar.gz" -C "$(dirname "$EV")" "$(basename "$EV")"
  echo "bundle: $HOME/$(basename "$EV").tar.gz ($(wc -l < "$EV/SHA256SUMS") files)"
  ;;

*) echo "usage: bash $0 {measure|collect}"; exit 1 ;;
esac
