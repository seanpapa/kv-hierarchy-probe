#!/usr/bin/env bash
# Gate 3 (admission cost) — measure/collect.
# Usage: bash g3_admission_cost.sh {measure|collect}
#
# Preconditions (bring the arms up with g2_context_sweep.sh, one source of config):
#   1. Recompute arm serving on RECO_PORT (no KV connector).
#   2. GDS arm serving on PORT after `bash g2_context_sweep.sh setup` passed gate 1.
# This script does not start containers; it drives g3_admission_cost.py against the
# arms g2_context_sweep.sh already validated, then collects checksummed evidence.
set -uo pipefail
# Which GPU this run measures. Storage counters do not identify the accelerator
# that issued I/O: nvidia-fs is host-wide, while cgroup accounting is filtered
# to the recorded tier block device. Record device_id (and gpu_index if stated)
# in the configuration runtime block as well.
export PROBE_GPU_INDEX="${PROBE_GPU_INDEX:-0}"

PORT=${PORT:-8102}          # GDS arm (g2_context_sweep.sh CTNR)
RECO_PORT=${RECO_PORT:-8100} # recompute arm
# The client next to this script, not a fixed path in /tmp another user could write first.
CLIENT=${CLIENT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/g3_admission_cost.py}
CONTEXTS=${CONTEXTS:-2048 4096}
# A fresh EVIDENCE per run keeps an older run's files out of this one.
EV=${EVIDENCE:-/tmp/admission-cost-evidence}
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
  curl -sf "http://127.0.0.1:$RECO_PORT/v1/models" >/dev/null \
    || { echo "!! recompute arm not responding on $RECO_PORT (start it first)"; exit 1; }
  curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null \
    || { echo "!! GDS arm not responding on $PORT (run: bash g2_context_sweep.sh setup)"; exit 1; }
  # shellcheck disable=SC2086
  python3 "$CLIENT" --gds-port "$PORT" --recompute-port "$RECO_PORT" \
    --contexts $CONTEXTS --samples "${N:-10}" --warmup "${WARMUP:-2}" \
    --fillers "${FILLERS:-6}" --filler-tokens "${FILLER_TOKENS:-8192}" \
    --output "$EV/admission-cost.json" 2>&1 | tee "$EV/measure-console.txt"
  ;;

collect)
  cp "$CLIENT" "$EV"/ 2>/dev/null
  { date -u; uname -a; nvidia-smi --query-gpu=index,name,pci.bus_id --format=csv; echo "measured_device_id=$(nvidia-smi -i "$PROBE_GPU_INDEX" --query-gpu=pci.bus_id --format=csv,noheader | tr -d ' ')"; echo "measured_gpu_index=$PROBE_GPU_INDEX"; } > "$EV/provenance.txt"
  ( cd "$EV" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | sort | xargs sha256sum > SHA256SUMS )
  tar czf "$HOME/$(basename "$EV").tar.gz" -C "$(dirname "$EV")" "$(basename "$EV")"
  echo "bundle: $HOME/$(basename "$EV").tar.gz ($(wc -l < "$EV/SHA256SUMS") files)"
  ;;

*) echo "usage: bash $0 {measure|collect}"; exit 1 ;;
esac
