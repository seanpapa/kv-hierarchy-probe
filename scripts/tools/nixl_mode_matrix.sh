#!/usr/bin/env bash
# LMCache NIXL FILE-mode matrix: POSIX/GDS_MT x static/dynamic.
# Usage: bash nixl_mode_matrix.sh {preflight|setup|measure|matched|collect|cleanup|smoke-gds}
#
# This is intentionally separate from nixl_matched.sh. That script reproduces
# the LMCache v0.4.1 static-pool matched result; this one requires a newer LMCache
# build with NixlDynamicStorageBackend FILE support (PR #2936 or later).
set -uo pipefail

export PROBE_GPU_INDEX="${PROBE_GPU_INDEX:-0}"

IMAGE=${IMAGE:?set IMAGE to the vLLM+LMCache image under test}
GDS_MOUNT=${GDS_MOUNT:?set GDS_MOUNT to the target filesystem mount}
MODEL=${MODEL:-/models/Qwen2.5-32B-Instruct}
export PROBE_MODEL="${PROBE_MODEL:-$(basename "$MODEL")}"
# The measurement client: next to this script when copied flat, else in the parent scripts/
# directory of a checkout — never a fixed path in /tmp that another user could write first.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ -z "${CLIENT:-}" ]]; then
  CLIENT=$HERE/g5_controlled_kv_experiment.py
  [[ -f "$CLIENT" ]] || CLIENT=$HERE/../g5_controlled_kv_experiment.py
fi
# The serving API has no authentication, so it listens on loopback only. SERVE_HOST opens it
# to other interfaces — set it only on a network you control.
SERVE_HOST=${SERVE_HOST:-127.0.0.1}
GDSCHECK=${GDSCHECK:-/usr/local/cuda/gds/tools/gdscheck}

NIXL_STORAGE_MODE=${NIXL_STORAGE_MODE:-static}
NIXL_BACKEND=${NIXL_BACKEND:-GDS_MT}
NIXL_STATIC_POOL_SIZE=${NIXL_STATIC_POOL_SIZE:-512}
NIXL_BUFFER_BYTES=${NIXL_BUFFER_BYTES:-3221225472}
NIXL_PRESENCE_CACHE=${NIXL_PRESENCE_CACHE:-false}
NIXL_USE_DIRECT_IO=${NIXL_USE_DIRECT_IO:-}
L1_GB=${L1_GB:-3}
CONTEXTS=${CONTEXTS:-4096}

case "$NIXL_STORAGE_MODE" in
  static) NIXL_POOL_SIZE=$NIXL_STATIC_POOL_SIZE ;;
  dynamic) NIXL_POOL_SIZE=0 ;;
  *) echo "!! NIXL_STORAGE_MODE must be static or dynamic" >&2; exit 2 ;;
esac

[[ "$NIXL_STATIC_POOL_SIZE" =~ ^[1-9][0-9]*$ ]] \
  || { echo "!! NIXL_STATIC_POOL_SIZE must be a positive integer" >&2; exit 2; }

case "$NIXL_BACKEND" in
  POSIX)
    ATTRIBUTION=posix
    NIXL_BUFFER_DEVICE=cpu
    NIXL_USE_DIRECT_IO=${NIXL_USE_DIRECT_IO:-false}
    ;;
  GDS_MT)
    ATTRIBUTION=gds
    NIXL_BUFFER_DEVICE=cuda
    NIXL_USE_DIRECT_IO=${NIXL_USE_DIRECT_IO:-true}
    ;;
  GDS)
    ATTRIBUTION=gds
    NIXL_BUFFER_DEVICE=cuda
    NIXL_USE_DIRECT_IO=${NIXL_USE_DIRECT_IO:-true}
    ;;
  *) echo "!! NIXL_BACKEND must be POSIX, GDS_MT, or GDS" >&2; exit 2 ;;
esac

case "$NIXL_PRESENCE_CACHE:$NIXL_USE_DIRECT_IO" in
  true:true|true:false|false:true|false:false) ;;
  *) echo "!! NIXL_PRESENCE_CACHE and NIXL_USE_DIRECT_IO must be true or false" >&2; exit 2 ;;
esac

BACKEND_LOWER=$(printf '%s' "$NIXL_BACKEND" | tr '[:upper:]' '[:lower:]')
ARM_ID="nixl_${NIXL_STORAGE_MODE}_${BACKEND_LOWER}"
BACKEND_ID="lmcache_${ARM_ID}"
TIER_NAME="lmcache-${ARM_ID}"
TIER_HOST="$GDS_MOUNT/$TIER_NAME"
TIER_CONTAINER="/gds/$TIER_NAME"
CTNR=${CTNR:-kv-${ARM_ID}}
PORT=${PORT:-8106}
EV=${EVIDENCE:-/tmp/$ARM_ID}

# Every container this launcher starts carries this label, and only a labelled container is
# ever removed: a same-named container someone else started is left alone.
PROBE_OWNER=kv-hierarchy-probe

remove_owned_container() {
  local name=$1 owner
  owner=$(docker inspect --format '{{ index .Config.Labels "kvprobe.owner" }}' "$name" 2>/dev/null) || return 0
  if [[ "$owner" != "$PROBE_OWNER" ]]; then
    echo "!! container $name exists and was not started by $PROBE_OWNER; leaving it." >&2
    echo "   If it is safe to remove: docker rm -f $name" >&2
    return 1
  fi
  docker rm -f "$name" >/dev/null
}

# Evidence lands in a directory this operator owns; a pre-placed symlink would redirect it.
prepare_evidence_dir() {
  local dir=$1
  [[ ! -L "$dir" ]] || { echo "!! evidence path is a symlink: $dir" >&2; return 1; }
  mkdir -p "$dir"
  [[ -O "$dir" ]] || { echo "!! evidence path is not owned by $(id -un): $dir" >&2; return 1; }
}

prepare_evidence_dir "$EV" || exit 1

nvfs_writes() {
  grep -m1 '^Writes' /proc/driver/nvidia-fs/stats 2>/dev/null \
    | grep -o 'n=[0-9]*' | cut -d= -f2
}

cgroup_writes() {
  local pid relative
  pid=$(docker inspect "$CTNR" --format '{{.State.Pid}}' 2>/dev/null || true)
  [[ "$pid" =~ ^[0-9]+$ ]] || { printf '%s\n' -1; return; }
  relative=$(awk -F:: 'NR==1 {print $2}' "/proc/$pid/cgroup" 2>/dev/null)
  [[ -n "$relative" ]] || { printf '%s\n' -1; return; }
  awk '{for (i=2; i<=NF; i++) if ($i ~ /^wbytes=/) {split($i,a,"="); s+=a[2]}} END {print s+0}' \
    "/sys/fs/cgroup/${relative#/}/io.stat" 2>/dev/null
}

tree_stats() {
  local count bytes nonzero
  count=$(find "$TIER_HOST" -type f 2>/dev/null | wc -l)
  bytes=$(find "$TIER_HOST" -type f -printf '%s\n' 2>/dev/null \
    | awk '{s+=$1} END {print s+0}')
  nonzero=$(find "$TIER_HOST" -type f -size +0c 2>/dev/null | wc -l)
  printf '%s %s %s\n' "$count" "$bytes" "$nonzero"
}

model_mount() {
  local src value
  for src in vllm-baseline vllm-lmcache-sweep kv-controlled; do
    value=$(docker inspect "$src" --format \
      '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' \
      2>/dev/null || true)
    [[ -n "$value" ]] && { printf '%s\n' "$value"; return 0; }
  done
  return 1
}

preflight() {
  [[ -f "$CLIENT" ]] || { echo "!! copy g5_controlled_kv_experiment.py to $CLIENT"; return 1; }
  docker run --rm "$IMAGE" python3 -c 'import nixl._api; print("nixl._api import OK")' \
    || { echo "!! nixl package is unavailable"; return 1; }
  if [[ "$NIXL_STORAGE_MODE" == dynamic ]]; then
    docker run --rm "$IMAGE" python3 -c \
      'import inspect; from lmcache.v1.storage_backend.nixl_storage_backend import NixlDynamicStorageBackend as C; s=inspect.getsource(C); assert "mem_type == \"FILE\"" in s and "os.open" in s; print("dynamic FILE implementation OK")' \
      || { echo "!! image lacks dynamic FILE support; use PR #2936 or later"; return 1; }
  fi
  if [[ "$ATTRIBUTION" == gds ]]; then
    ls /dev/nvidia-fs* >/dev/null 2>&1 || { echo "!! no /dev/nvidia-fs*"; return 1; }
    [[ -r /etc/cufile.json ]] || { echo "!! /etc/cufile.json is not readable"; return 1; }
    local output
    output=$( { command -v gdscheck >/dev/null 2>&1 && gdscheck -p; } 2>/dev/null \
      || "$GDSCHECK" -p 2>/dev/null )
    grep -qiE 'use_compat_mode[[:space:]]*:[[:space:]]*false' <<<"$output" \
      || { echo "!! use_compat_mode:false not confirmed"; return 1; }
  fi
  echo "preflight PASS: arm=$ARM_ID backend=$NIXL_BACKEND mode=$NIXL_STORAGE_MODE"
}

setup() {
  preflight || return 1
  sudo -v || return 1
  # Emptying the tier runs as root: the mount must really be a mount, and the tier directory
  # must not be a symlink to somewhere else.
  mountpoint -q "$GDS_MOUNT" || { echo "!! not a mount point: $GDS_MOUNT"; return 1; }
  [[ ! -L "$TIER_HOST" ]] || { echo "!! tier directory is a symlink: $TIER_HOST"; return 1; }
  sudo mkdir -p "$TIER_HOST"
  sudo find "$TIER_HOST" -mindepth 1 -delete
  sudo chown "$(id -u):$(id -g)" "$TIER_HOST"

  local model_host gpu_used extra
  model_host=$(model_mount) || { echo "!! model mount not found"; return 1; }
  for old in "$CTNR" vllm-lmcache-sweep kv-controlled vllm-recompute-gpu0 vllm-lmcache-gds; do
    if docker inspect "$old" >/dev/null 2>&1; then
      remove_owned_container "$old" || return 1
      echo "removed experiment container $old"
    fi
  done
  gpu_used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits \
    -i "$PROBE_GPU_INDEX" | tr -d ' ')
  [[ ${gpu_used:-0} -le 5000 ]] \
    || { echo "!! GPU${PROBE_GPU_INDEX} still uses ${gpu_used} MiB"; return 1; }

  extra=$(printf \
    '{"enable_nixl_storage":true,"nixl_backend":"%s","nixl_pool_size":%s,"nixl_path":"%s","nixl_presence_cache":%s,"use_direct_io":%s}' \
    "$NIXL_BACKEND" "$NIXL_POOL_SIZE" "$TIER_CONTAINER" \
    "$NIXL_PRESENCE_CACHE" "$NIXL_USE_DIRECT_IO")

  local dev_args=() gds_mount_args=()
  if [[ "$ATTRIBUTION" == gds ]]; then
    local device
    for device in /dev/nvidia-fs*; do
      [[ -e "$device" ]] && dev_args+=(--device "$device")
    done
    gds_mount_args+=(
      --volume /run/udev:/run/udev:ro
      --volume /etc/cufile.json:/etc/cufile.json:ro
      --env CUFILE_ENV_PATH_JSON=/etc/cufile.json
    )
  fi

  docker run -d --name "$CTNR" --label "kvprobe.owner=$PROBE_OWNER" \
    --gpus "\"device=${PROBE_GPU_INDEX}\"" --ipc host --cap-add IPC_LOCK \
    --ulimit memlock=-1 --network host \
    "${dev_args[@]}" "${gds_mount_args[@]}" \
    --volume "$GDS_MOUNT":/gds:rw \
    --volume "$model_host":/models:ro \
    --env LMCACHE_CHUNK_SIZE=256 \
    --env LMCACHE_LOCAL_CPU=True \
    --env LMCACHE_MAX_LOCAL_CPU_SIZE="$L1_GB" \
    --env LMCACHE_SAVE_UNFULL_CHUNK=False \
    --env LMCACHE_NIXL_BUFFER_SIZE="$NIXL_BUFFER_BYTES" \
    --env LMCACHE_NIXL_BUFFER_DEVICE="$NIXL_BUFFER_DEVICE" \
    --env LMCACHE_EXTRA_CONFIG="$extra" \
    --env PYTHONHASHSEED=0 \
    --env LMCACHE_LOG_LEVEL="${LOGLEVEL:-INFO}" \
    "$IMAGE" \
    vllm serve "$MODEL" --host "$SERVE_HOST" --port "$PORT" \
      --served-model-name "$PROBE_MODEL" \
      --gpu-memory-utilization 0.80 --no-enable-prefix-caching \
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
    >/dev/null || return 1

  {
    echo "arm=$ARM_ID"
    echo "backend=$BACKEND_ID"
    echo "attribution=$ATTRIBUTION"
    echo "nixl_storage_mode=$NIXL_STORAGE_MODE"
    echo "nixl_backend=$NIXL_BACKEND"
    echo "nixl_pool_size=$NIXL_POOL_SIZE"
    echo "nixl_buffer_device=$NIXL_BUFFER_DEVICE"
    echo "nixl_buffer_bytes=$NIXL_BUFFER_BYTES"
    echo "nixl_presence_cache=$NIXL_PRESENCE_CACHE"
    echo "use_direct_io=$NIXL_USE_DIRECT_IO"
    echo "extra_config=$extra"
  } | tee "$EV/runtime-config.txt"

  echo -n "waiting for model load"
  local i
  for i in $(seq 1 80); do
    curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1 \
      && { echo " -> ready ($((i*5))s)"; break; }
    docker ps --format '{{.Names}}' | grep -qx "$CTNR" \
      || { echo; docker logs --tail 60 "$CTNR"; return 1; }
    echo -n "."
    sleep 5
  done
  curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null \
    || { echo "!! startup timeout"; return 1; }
  docker logs "$CTNR" 2>&1 | grep -iE \
    'nixl|storage backend|created backend|GDS|POSIX|error=5027|file register' \
    | tee "$EV/backend-init.txt" | tail -20
  docker logs "$CTNR" 2>&1 | grep -q 'Failed during post_init' \
    && { echo "!! backend initialization failed"; return 1; }

  write_gate
}

write_gate() {
  local count0 bytes0 nonzero0 count1 bytes1 nonzero1 counter0 counter1 errors
  read -r count0 bytes0 nonzero0 < <(tree_stats)
  if [[ "$NIXL_STORAGE_MODE" == static && "$count0" -lt "$NIXL_POOL_SIZE" ]]; then
    echo "!! static pool has $count0 files, expected at least $NIXL_POOL_SIZE"; return 1
  fi
  if [[ "$NIXL_STORAGE_MODE" == dynamic && "$count0" -ne 0 ]]; then
    echo "!! dynamic directory must start empty; found $count0 files"; return 1
  fi
  if [[ "$ATTRIBUTION" == gds ]]; then counter0=$(nvfs_writes); else counter0=$(cgroup_writes); fi
  [[ "$counter0" =~ ^-?[0-9]+$ ]] || { echo "!! write counter unavailable"; return 1; }

  PORT="$PORT" PROBE_MODEL="$PROBE_MODEL" python3 - <<'PY'
import json
import os
import subprocess
import tempfile

sentence = (
    "LMCache reuses previously computed key-value cache blocks when the same long "
    "context appears again. This deterministic sentence exists only to control "
    "the token length of this storage experiment. "
)
text = sentence * 100 + (
    "\n\nDo not summarize the context. Reply with exactly 'STAGE1_D6_OK'."
)
body = {
    "model": os.environ["PROBE_MODEL"],
    "messages": [{"role": "user", "content": text}],
    "max_tokens": 7,
    "temperature": 0,
}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
    json.dump(body, handle)
    path = handle.name
try:
    result = subprocess.run(
        [
            "curl", "-s", "--max-time", "600", "-X", "POST",
            f"http://127.0.0.1:{os.environ['PORT']}/v1/chat/completions",
            "-H", "Content-Type: application/json", "--data-binary", "@" + path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    print(result.stdout[:200] if result.stdout else result.stderr[:200])
finally:
    os.unlink(path)
PY

  if [[ "$ATTRIBUTION" == posix ]]; then
    docker exec "$CTNR" sync
  fi
  local i
  counter1=$counter0
  for i in $(seq 1 15); do
    sleep 2
    if [[ "$ATTRIBUTION" == gds ]]; then counter1=$(nvfs_writes); else counter1=$(cgroup_writes); fi
    read -r count1 bytes1 nonzero1 < <(tree_stats)
    [[ "$counter1" =~ ^-?[0-9]+$ ]] || continue
    [[ "$counter1" -gt "$counter0" && "$bytes1" -gt "$bytes0" ]] && break
  done
  read -r count1 bytes1 nonzero1 < <(tree_stats)
  errors=$(docker logs "$CTNR" 2>&1 | grep -ciE \
    'error=5027|file register error|NIXL_ERR_BACKEND|Error in setting up Batch')
  {
    echo "counter_kind=$ATTRIBUTION"
    echo "write_counter_delta=$((counter1-counter0))"
    echo "files_before=$count0"
    echo "files_after=$count1"
    echo "file_bytes_delta=$((bytes1-bytes0))"
    echo "nonzero_files_after=$nonzero1"
    echo "nixl_error_count=$errors"
  } | tee "$EV/gate.txt"

  [[ "$errors" -eq 0 ]] || { echo "!! NIXL/cuFile error found"; return 1; }
  [[ "$counter1" -gt "$counter0" ]] || { echo "!! no attributed write observed"; return 1; }
  [[ "$bytes1" -gt "$bytes0" ]] || { echo "!! backing-file bytes did not grow"; return 1; }
  if [[ "$NIXL_STORAGE_MODE" == dynamic && "$count1" -le "$count0" ]]; then
    echo "!! dynamic store created no key-derived files"; return 1
  fi
  echo "gate_pass=1" | tee -a "$EV/gate.txt"
}

measure() {
  [[ "$NIXL_BACKEND" != GDS ]] \
    || { echo "!! NIXL GDS is smoke-only; primary measurements use GDS_MT"; return 2; }
  grep -qx 'gate_pass=1' "$EV/gate.txt" 2>/dev/null \
    || { echo "!! setup/write gate has not passed"; return 1; }
  curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null \
    || { echo "!! arm is not serving"; return 1; }
  local output=${OUTPUT:-$EV/${ARM_ID}-context.json}
  local console=${MEASURE_CONSOLE:-$EV/measure-console.txt}
  PROBE_POSIX_TIER_DIR="$TIER_CONTAINER" python3 "$CLIENT" context \
    --arm "$ARM_ID" --attribution "$ATTRIBUTION" --backend "$BACKEND_ID" \
    --port "$PORT" --container "$CTNR" --output "$output" \
    --contexts $CONTEXTS --samples "${N:-10}" --warmup "${WARMUP:-2}" \
    --fillers "${FILLERS:-6}" --filler-key-policy reuse 2>&1 | tee "$console"
}

matched() {
  local run
  for run in 1 2 3; do
    OUTPUT="$EV/${ARM_ID}-run${run}.json" \
      MEASURE_CONSOLE="$EV/${ARM_ID}-run${run}-console.txt" \
      N=10 WARMUP=2 measure || return 1
  done
}

collect() {
  cp "$CLIENT" "$0" "$EV/" 2>/dev/null || true
  docker inspect "$CTNR" > "$EV/container-inspect.json" 2>&1
  docker logs "$CTNR" > "$EV/container-full.log" 2>&1
  gzip -9f "$EV/container-full.log"
  docker image inspect "$IMAGE" --format '{{.Id}} {{json .RepoDigests}}' \
    > "$EV/image-identity.txt" 2>&1
  docker exec -i "$CTNR" python3 - <<'PY' > "$EV/software-versions.txt" 2>&1
from importlib.metadata import PackageNotFoundError, version
for package in ("lmcache", "nixl", "nixl-cu13", "vllm"):
    try:
        print(f"{package}={version(package)}")
    except PackageNotFoundError:
        pass
PY
  {
    date -u
    uname -a
    findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS "$GDS_MOUNT"
    nvidia-smi --query-gpu=index,name,pci.bus_id,driver_version --format=csv
  } > "$EV/provenance.txt"
  (cd "$EV" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS)
  local bundle="$HOME/$(basename "$EV").tar.gz"
  tar czf "$bundle" -C "$(dirname "$EV")" "$(basename "$EV")"
  echo "bundle: $bundle"
}

command=${1:-}
case "$command" in
  preflight) preflight ;;
  setup) setup ;;
  measure) measure ;;
  matched) matched ;;
  collect) collect ;;
  cleanup) remove_owned_container "$CTNR" && echo "$CTNR removed" ;;
  smoke-gds)
    [[ "$NIXL_BACKEND" == GDS ]] \
      || { echo "usage: NIXL_BACKEND=GDS bash $0 smoke-gds"; exit 2; }
    setup
    ;;
  *)
    echo "usage: bash $0 {preflight|setup|measure|matched|collect|cleanup|smoke-gds}" >&2
    exit 2
    ;;
esac
