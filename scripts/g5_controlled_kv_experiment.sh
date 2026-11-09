#!/usr/bin/env bash
# Controlled single-GPU experiment (PROBE_GPU_INDEX, default 0): recompute / buffered POSIX / native GDS.
# Usage: bash g5_controlled_kv_experiment.sh
#   {preflight|serve|stop|one|posix-sweep|randomized|matched-gds|context-sweep|concurrency|concurrency-gds|collect}
set -euo pipefail

# Required — these name your image and your GDS-capable mount, so there is no default.
# GDS_MOUNT is the canonical name shared with g2_context_sweep.sh; MOUNT is accepted as an alias.
IMAGE=${IMAGE:?set IMAGE to the vLLM+LMCache container image built for this host}
MOUNT=${MOUNT:-${GDS_MOUNT:-}}
MOUNT=${MOUNT:?set GDS_MOUNT (or MOUNT) to the GDS-capable filesystem mount on this host}

MODEL=${MODEL:-/models/Qwen2.5-32B-Instruct}
# Which GPU this run measures. Storage counters do not identify the accelerator
# that issued I/O: nvidia-fs is host-wide, while cgroup accounting is filtered
# to the recorded tier block device. Record device_id (and gpu_index if stated)
# in the configuration runtime block as well.
export PROBE_GPU_INDEX="${PROBE_GPU_INDEX:-0}"
# The client sends this exact name in the request "model" field and it must equal
# vLLM's --served-model-name below. Default to the model directory basename so a
# single MODEL override reparameterizes both. PROBE_MIB_PER_CHUNK sets the KV
# geometry the client's tier_ok gate expects (see the .py). Both are exported so
# the client subprocess inherits them.
export PROBE_MODEL="${PROBE_MODEL:-$(basename "$MODEL")}"
export PROBE_MIB_PER_CHUNK="${PROBE_MIB_PER_CHUNK:-64}"
# Size of LMCache's cuFile buffer pool, in MiB, for the gds arm. It is a tier
# sizing knob, not a tuning dial: the pool bounds how many concurrent requests
# can be served from the device at once, so it participates in gate 5. Keep it
# below the working set — a pool larger than the data becomes a GPU-resident
# tier and the run never reaches the device (see docs/FAILURE-CATALOG.md).
PROBE_CUFILE_BUF_MIB=${PROBE_CUFILE_BUF_MIB:-3072}
# Where the POSIX arm's tier lives inside the container. Exported so the client
# evicts the same directory this script creates and hands to LMCache — one
# variable drives both, because two that must agree will eventually disagree.
export PROBE_POSIX_TIER_DIR="${PROBE_POSIX_TIER_DIR:-/gds/lmcache-controlled-posix}"
# The POSIX tier is emptied before each arm starts, so it must be a directory of its own one
# level under the mount. /gds itself, or a path that climbs out of it, would empty the mount.
check_posix_tier_dir() {
  local dir=$1
  [[ "$dir" =~ ^/gds/[A-Za-z0-9._-]+$ && "$dir" != /gds/. && "$dir" != /gds/.. ]] || {
    echo "!! PROBE_POSIX_TIER_DIR must be one directory under /gds (e.g. /gds/lmcache-controlled-posix): $dir" >&2
    return 1
  }
}
# Host path of the tier filesystem. The client resolves it to one block device
# so the cgroup counter is read for that device instead of summing every device
# the container touched. PROBE_POSIX_TIER_DIR is a container path and cannot
# answer this; the client runs on the host.
export PROBE_TIER_MOUNT="$MOUNT"
PORT=${PORT:-8104}
# The serving API has no authentication, so it listens on loopback only. SERVE_HOST opens it
# to other interfaces — set it only on a network you control.
SERVE_HOST=${SERVE_HOST:-127.0.0.1}
CONTAINER=${CONTAINER:-kv-controlled}
EVIDENCE=${EVIDENCE:-/tmp/kv-controlled-20260827}
# The client next to this script, not a fixed path in /tmp that another user could write first.
CLIENT=${CLIENT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/g5_controlled_kv_experiment.py}
SEED=${SEED:-20260827}
FILLER_KEY_POLICY=${FILLER_KEY_POLICY:-unique}
# Tokens per filler request. The default keeps every recorded run comparable; a smaller
# value keeps each filler within a small staging pool (one filler must not exceed it).
FILLER_TOKENS=${FILLER_TOKENS:-8192}
PROMPT_PROFILE=${PROMPT_PROFILE:-transport}
PROMPT_SEED=${PROMPT_SEED:-17}
NEEDLE_POSITION=${NEEDLE_POSITION:-early}
PROMPT_ARGS=(
  --prompt-profile "$PROMPT_PROFILE"
  --prompt-seed "$PROMPT_SEED"
  --needle-position "$NEEDLE_POSITION"
)
GDSCHECK=${GDSCHECK:-/usr/local/cuda-13.3/gds/tools/gdscheck}

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

check_posix_tier_dir "$PROBE_POSIX_TIER_DIR"
prepare_evidence_dir "$EVIDENCE"

# Host directory mounted at /models. MODEL_HOST_DIR names it; without it, it is read from
# the /models mount of an existing reference container.
model_mount() {
  if [[ -n "${MODEL_HOST_DIR:-}" ]]; then
    [[ -d "$MODEL_HOST_DIR" ]] || { echo "!! MODEL_HOST_DIR is not a directory: $MODEL_HOST_DIR" >&2; return 1; }
    printf '%s\n' "$MODEL_HOST_DIR"
    return
  fi
  for source in vllm-baseline vllm-lmcache-l2; do
    local value
    value=$(docker inspect "$source" --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)
    if [[ -n "$value" ]]; then
      printf '%s\n' "$value"
      return
    fi
  done
  return 1
}

wait_ready() {
  for _ in $(seq 1 120); do
    if curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null; then
      return
    fi
    docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
      docker logs --tail 80 "$CONTAINER"
      return 1
    }
    sleep 5
  done
  return 1
}

stop_arm() {
  remove_owned_container "$CONTAINER"
}

start_arm() {
  local arm=$1
  local host_models=$2
  stop_arm
  local gpu_used
  gpu_used=$(nvidia-smi -i "$PROBE_GPU_INDEX" --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')
  [[ ${gpu_used:-0} -lt 5000 ]] || {
    echo "GPU${PROBE_GPU_INDEX} is occupied: ${gpu_used} MiB" >&2
    docker ps --format '  {{.Names}}'
    return 1
  }

  local connector=()
  local storage=()
  if [[ "$arm" == posix ]]; then
    docker run --rm --volume "$MOUNT:/gds:rw" "$IMAGE" \
      bash -lc "rm -rf '$PROBE_POSIX_TIER_DIR'; mkdir -p '$PROBE_POSIX_TIER_DIR'"
    storage=(--volume "$MOUNT:/gds:rw"
      --env LMCACHE_LOCAL_CPU=True --env LMCACHE_MAX_LOCAL_CPU_SIZE=3
      --env LMCACHE_LOCAL_DISK="$PROBE_POSIX_TIER_DIR"
      --env LMCACHE_MAX_LOCAL_DISK_SIZE=256)
    connector=(--kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}')
  elif [[ "$arm" == gds ]]; then
    docker run --rm --volume "$MOUNT:/gds:rw" "$IMAGE" \
      bash -lc 'rm -rf /gds/lmcache-controlled-gds; mkdir -p /gds/lmcache-controlled-gds'
    local devices=()
    for device in /dev/nvidia-fs*; do
      [[ -e "$device" ]] && devices+=(--device "$device")
    done
    [[ ${#devices[@]} -gt 0 ]] || { echo "no nvidia-fs devices" >&2; return 1; }
    storage=("${devices[@]}" --cap-add IPC_LOCK --ulimit memlock=-1
      --volume /run/udev:/run/udev:ro --volume "$MOUNT:/gds:rw"
      --volume /etc/cufile.json:/etc/cufile.json:ro
      --env CUFILE_ENV_PATH_JSON=/etc/cufile.json
      --env LMCACHE_LOCAL_CPU=True --env LMCACHE_MAX_LOCAL_CPU_SIZE=3
      --env LMCACHE_GDS_PATH=/gds/lmcache-controlled-gds
      --env LMCACHE_CUFILE_BUFFER_SIZE="$PROBE_CUFILE_BUF_MIB"
      --env LMCACHE_MAX_LOCAL_DISK_SIZE=256)
    connector=(--kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}')
  fi

  docker run -d --name "$CONTAINER" --label "kvprobe.owner=$PROBE_OWNER" \
    --gpus "\"device=${PROBE_GPU_INDEX}\"" --ipc host --network host \
    --volume "$host_models:/models:ro" \
    --env LMCACHE_CHUNK_SIZE=256 --env LMCACHE_SAVE_UNFULL_CHUNK=False \
    --env PYTHONHASHSEED=0 "${storage[@]}" "$IMAGE" \
    vllm serve "$MODEL" --host "$SERVE_HOST" --port "$PORT" \
      --served-model-name "$PROBE_MODEL" --gpu-memory-utilization 0.80 \
      --no-enable-prefix-caching "${connector[@]}" >/dev/null
  wait_ready
  local api_root
  api_root=$(curl -sf "http://127.0.0.1:$PORT/v1/models" | python3 -c \
    'import json,sys; data=json.load(sys.stdin)["data"]; print(data[0].get("root", "") if data else "")')
  [[ "$api_root" == "$MODEL" ]] || {
    echo "model identity mismatch: API root=$api_root expected=$MODEL (served name=$PROBE_MODEL)" >&2
    docker logs --tail 80 "$CONTAINER" >&2
    return 1
  }
  docker inspect "$CONTAINER" > "$EVIDENCE/${arm}-container-inspect-latest.json"
  docker logs "$CONTAINER" > "$EVIDENCE/${arm}-startup-latest.log" 2>&1
  if [[ "$arm" != recompute ]]; then
    grep -q 'LMCacheEngine' "$EVIDENCE/${arm}-startup-latest.log" || {
      echo "LMCache did not initialize" >&2
      return 1
    }
  fi
  if [[ "$arm" == gds ]]; then
    grep -q 'GdsBackend' "$EVIDENCE/${arm}-startup-latest.log" || {
      echo "GDS backend did not initialize" >&2
      return 1
    }
    grep -q 'properties.use_compat_mode : false' < <("$GDSCHECK" -p) || {
      echo "compat mode is not disabled" >&2
      return 1
    }
  fi
}

backend_id_for_arm() {
  case "$1" in
    recompute) printf '%s\n' disabled ;;
    posix) printf '%s\n' lmcache_native_posix ;;
    gds) printf '%s\n' lmcache_native_gds ;;
    *) echo "unknown native arm: $1" >&2; return 2 ;;
  esac
}

run_context() {
  local arm=$1
  local output=$2
  python3 "$CLIENT" context --arm "$arm" --port "$PORT" \
    --backend "$(backend_id_for_arm "$arm")" \
    --container "$([[ "$arm" == recompute ]] || printf '%s' "$CONTAINER")" \
    --output "$output" --contexts 256 512 1024 2048 4096 \
    --samples "${SAMPLES:-10}" --warmup 2 --fillers "${FILLERS:-6}" --filler-tokens "${FILLER_TOKENS:-8192}" \
    "${PROMPT_ARGS[@]}" \
    --filler-key-policy "$FILLER_KEY_POLICY"
}

run_representative() {
  local arm=$1
  local run=$2
  python3 "$CLIENT" context --arm "$arm" --port "$PORT" \
    --backend "$(backend_id_for_arm "$arm")" \
    --container "$([[ "$arm" == recompute ]] || printf '%s' "$CONTAINER")" \
    --output "$EVIDENCE/randomized-run${run}-${arm}.json" --contexts "${REP_CONTEXT:-4096}" \
    --samples "${SAMPLES:-10}" --warmup 2 --fillers "${FILLERS:-6}" --filler-tokens "${FILLER_TOKENS:-8192}" \
    "${PROMPT_ARGS[@]}" \
    --filler-key-policy "$FILLER_KEY_POLICY"
}

case ${1:-} in
preflight)
  [[ -f "$CLIENT" ]] || { echo "copy client to $CLIENT" >&2; exit 1; }
  host_models=$(model_mount) || { echo "model mount not found" >&2; exit 1; }
  [[ -d "$host_models/$PROBE_MODEL" ]]
  findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS "$MOUNT"
  "$GDSCHECK" -p | grep -E 'NVMe|use_compat_mode'
  nvidia-smi --query-gpu=index,name,pci.bus_id,memory.used --format=csv
  docker image inspect "$IMAGE" --format '{{.Id}} {{json .RepoDigests}}'
  ;;
posix-sweep)
  host_models=$(model_mount)
  start_arm posix "$host_models"
  run_context posix "$EVIDENCE/posix-context-sweep.json"
  ;;
randomized)
  host_models=$(model_mount)
  mapfile -t schedule < <(python3 - "$SEED" <<'PY'
import random
import sys
rng = random.Random(int(sys.argv[1]))
for run in range(1, 4):
    arms = ["recompute", "posix", "gds"]
    rng.shuffle(arms)
    for arm in arms:
        print(f"{run} {arm}")
PY
  )
  printf '%s\n' "${schedule[@]}" | tee "$EVIDENCE/randomized-schedule.txt"
  for item in "${schedule[@]}"; do
    read -r run arm <<<"$item"
    start_arm "$arm" "$host_models"
    run_representative "$arm" "$run"
    docker logs "$CONTAINER" > "$EVIDENCE/randomized-run${run}-${arm}-container.log" 2>&1
  done
  ;;
serve)
  # Start one arm and leave it serving, for clients this script does not drive itself
  # (g3_admission_cost.sh, g4_capacity_sweep.sh) or as a second arm on another GPU.
  # Usage: bash $0 serve ARM   (ARM = recompute | posix | gds); stop it with `bash $0 stop`.
  arm=${2:?"usage: bash $0 serve ARM -- ARM is recompute, posix or gds"}
  host_models=$(model_mount)
  start_arm "$arm" "$host_models"
  echo "serving $arm as $CONTAINER on 127.0.0.1:$PORT (GPU $PROBE_GPU_INDEX)"
  ;;
stop)
  stop_arm
  ;;
one)
  # Single arm, single representative run. `randomized` fixes its own three-arm schedule
  # internally, so an external schedule that interleaves a fourth arm needs this unit.
  # Usage: bash $0 one ARM RUN   (ARM = recompute | posix | gds)
  arm=${2:?"usage: bash $0 one ARM RUN -- ARM is recompute, posix or gds"}
  run=${3:?"usage: bash $0 one ARM RUN -- RUN is the run index"}
  host_models=$(model_mount)
  start_arm "$arm" "$host_models"
  run_representative "$arm" "$run"
  docker logs "$CONTAINER" > "$EVIDENCE/randomized-run${run}-${arm}-container.log" 2>&1
  ;;
matched-gds)
  host_models=$(model_mount)
  start_arm gds "$host_models"
  for run in 1 2 3; do
    run_representative gds "$run"
    docker logs "$CONTAINER" > "$EVIDENCE/matched-run${run}-gds-container.log" 2>&1
  done
  ;;
context-sweep)
  # Full three-arm context sweep in one command (Gate 2 break-even + Gate 1
  # per-request attribution for recompute / buffered POSIX / native GDS).
  # CONTEXTS is space-separated (argparse nargs), e.g. CONTEXTS="256 512 1024 2048 4096 8192".
  host_models=$(model_mount)
  for arm in recompute posix gds; do
    start_arm "$arm" "$host_models"
    python3 "$CLIENT" context --arm "$arm" --port "$PORT" \
      --backend "$(backend_id_for_arm "$arm")" \
      --container "$([[ "$arm" == recompute ]] || printf '%s' "$CONTAINER")" \
      --output "$EVIDENCE/context-${arm}.json" \
      --contexts ${CONTEXTS:-256 512 1024 2048 4096} \
      --samples "${SAMPLES:-10}" --warmup 2 --fillers "${FILLERS:-6}" --filler-tokens "${FILLER_TOKENS:-8192}" \
      "${PROMPT_ARGS[@]}" \
      --filler-key-policy "$FILLER_KEY_POLICY"
    docker logs "$CONTAINER" > "$EVIDENCE/context-${arm}-container.log" 2>&1 || true
  done
  ;;
concurrency)
  host_models=$(model_mount)
  for arm in recompute posix gds; do
    start_arm "$arm" "$host_models"
    python3 "$CLIENT" concurrency --arm "$arm" --port "$PORT" \
      --backend "$(backend_id_for_arm "$arm")" \
      --container "$([[ "$arm" == recompute ]] || printf '%s' "$CONTAINER")" \
      --output "$EVIDENCE/concurrency-${arm}.json" --concurrency 1 2 4 8 \
      --concurrent-context 2048 --batches "${BATCHES:-10}" --warmup 2 \
      "${PROMPT_ARGS[@]}" \
      --fillers "${FILLERS:-6}" --filler-tokens "${FILLER_TOKENS:-8192}" --filler-key-policy "$FILLER_KEY_POLICY"
    docker logs "$CONTAINER" > "$EVIDENCE/concurrency-${arm}-container.log" 2>&1
  done
  ;;
concurrency-gds)
  host_models=$(model_mount)
  start_arm gds "$host_models"
  python3 "$CLIENT" concurrency --arm gds --port "$PORT" \
    --backend "$(backend_id_for_arm gds)" \
    --container "$CONTAINER" --output "$EVIDENCE/concurrency-gds.json" \
    --concurrency 1 2 4 8 --concurrent-context 2048 \
    --batches "${BATCHES:-10}" --warmup 2 --fillers "${FILLERS:-6}" --filler-tokens "${FILLER_TOKENS:-8192}" \
    "${PROMPT_ARGS[@]}" \
    --filler-key-policy "$FILLER_KEY_POLICY"
  docker logs "$CONTAINER" > "$EVIDENCE/concurrency-gds-container.log" 2>&1
  ;;
collect)
  cp "$CLIENT" "$0" "$EVIDENCE/"
  {
    date -Is
    uname -a
    nvidia-smi --query-gpu=index,name,pci.bus_id,driver_version --format=csv
    findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS "$MOUNT"
    git --version
    docker --version
  } > "$EVIDENCE/provenance.txt"
  (cd "$EVIDENCE" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
  tar -C "$(dirname "$EVIDENCE")" -czf "$HOME/$(basename "$EVIDENCE").tar.gz" "$(basename "$EVIDENCE")"
  echo "$HOME/$(basename "$EVIDENCE").tar.gz"
  ;;
*)
  echo "usage: bash $0 {preflight|posix-sweep|context-sweep|randomized|one|matched-gds|concurrency|concurrency-gds|collect}" >&2
  exit 2
  ;;
esac
