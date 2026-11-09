#!/usr/bin/env bash
# Context-length sweep — container start / gate / measure / collect
# Usage: bash g2_context_sweep.sh {setup|measure|collect|recompute0|cleanup}
set -uo pipefail

# Required — these name your image and your GDS-capable mount, so there is no default.
IMAGE=${IMAGE:?set IMAGE to the vLLM+LMCache container image built for this host}
GDS_MOUNT=${GDS_MOUNT:?set GDS_MOUNT to the GDS-capable filesystem mount on this host}

# Path inside the container. The host directory is discovered from the recompute arm below.
MODEL=${MODEL:-/models/Qwen2.5-32B-Instruct}
# The client's request "model" field must equal vLLM's --served-model-name. Default to
# the model directory basename so one MODEL override reparameterizes both. Exported so the
# python probe (which reads PROBE_MODEL / PROBE_MIB_PER_CHUNK) inherits the model geometry.
export PROBE_MODEL="${PROBE_MODEL:-$(basename "$MODEL")}"
export PROBE_MIB_PER_CHUNK="${PROBE_MIB_PER_CHUNK:-64}"
# Which GPU this run measures. Storage counters do not identify the accelerator
# that issued I/O: nvidia-fs is host-wide, while cgroup accounting is filtered
# to the recorded tier block device. Record device_id (and gpu_index if stated)
# in the configuration runtime block as well.
export PROBE_GPU_INDEX="${PROBE_GPU_INDEX:-0}"
CTNR=${CTNR:-vllm-lmcache-sweep}
PORT=${PORT:-8102}
RECO_CTNR=${RECO_CTNR:-vllm-baseline}
RECO_PORT=${RECO_PORT:-8100}
# Max context 8,192 tokens = 2 GiB KV. L1 and the pool must exceed that or the store is truncated.
L1_GB=${L1_GB:-3}
CUFILE_BUF_MIB=${CUFILE_BUF_MIB:-3072}
# A fresh EVIDENCE per run keeps an older run's files out of this one.
EV=${EVIDENCE:-/tmp/ctx-sweep-evidence}
# The serving API has no authentication, so it is reachable on loopback only. SERVE_HOST opens
# it to other interfaces — set it only on a network you control.
SERVE_HOST=${SERVE_HOST:-127.0.0.1}
# The measurement client next to this script, not a fixed path in /tmp another user could write.
CLIENT=${CLIENT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/g2_context_sweep.py}

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

# Recreating a tier directory runs as root, so the mount must really be a mount and the
# directory must not be a symlink to somewhere else.
reset_tier_dir() {
  local mount=$1 dir=$2
  mountpoint -q "$mount" || { echo "!! not a mount point: $mount" >&2; return 1; }
  [[ ! -L "$mount/$dir" ]] || { echo "!! tier directory is a symlink: $mount/$dir" >&2; return 1; }
  sudo rm -rf "${mount:?}/$dir" && sudo mkdir -p "$mount/$dir" && sudo chown "$(id -u):$(id -g)" "$mount/$dir"
}

prepare_evidence_dir "$EV" || exit 1

writes() { grep -m1 '^Writes' /proc/driver/nvidia-fs/stats | grep -o 'n=[0-9]*' | cut -d= -f2; }

case "${1:-}" in

setup)
  sudo -v || exit 1
  : > "$EV/gate.txt"
  DEV_ARGS=(); for d in /dev/nvidia-fs*; do [ -e "$d" ] && DEV_ARGS+=(--device "$d"); done
  [ ${#DEV_ARGS[@]} -eq 0 ] && { echo "!! no /dev/nvidia-fs*"; exit 1; }

  echo "### 0. preconditions"
  curl -sf "http://127.0.0.1:$RECO_PORT/v1/models" >/dev/null \
    && echo "  recompute arm($RECO_CTNR:$RECO_PORT) responding OK" \
    || { echo "!! $RECO_CTNR is not responding on $RECO_PORT (recompute arm required)"; exit 1; }
  MODEL_MOUNT=$(docker inspect "$RECO_CTNR" \
    --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}')
  NET_MODE=$(docker inspect "$RECO_CTNR" --format '{{.HostConfig.NetworkMode}}')
  # With host networking vLLM binds SERVE_HOST itself; in a bridge network it must listen on all
  # container interfaces and the host-side publish is what stays on SERVE_HOST.
  case "$NET_MODE" in
    host) NET_ARGS=(--network host); BIND=$SERVE_HOST ;;
    *) NET_ARGS=(--publish "$SERVE_HOST:$PORT:$PORT"); BIND=0.0.0.0 ;;
  esac
  echo "  model $MODEL_MOUNT / network $NET_MODE"

  # Free the target GPU — remove any leftover experiment containers, including a previous run's
  # own GDS arm ($CTNR): setup replaces it, and leaving it up blocks the occupancy check below
  # (so a changed CUFILE_BUF_MIB / L1_GB would silently not take effect).
  for old in "$CTNR" vllm-recompute-gpu0 vllm-lmcache-gds; do
    if docker inspect "$old" >/dev/null 2>&1; then
      remove_owned_container "$old" || exit 1
      echo "  removed leftover container $old"
    fi
  done
  GPU_USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$PROBE_GPU_INDEX" | tr -d ' ')
  echo "  GPU${PROBE_GPU_INDEX} in use: ${GPU_USED} MiB"
  [ "${GPU_USED:-0}" -gt 5000 ] && { echo "!! GPU${PROBE_GPU_INDEX} is still in use"; docker ps --format '  {{.Names}}'; exit 1; }

  echo; echo "### 1. clean gds_path"
  reset_tier_dir "$GDS_MOUNT" lmcache-sweep || exit 1
  [ "$(find "$GDS_MOUNT/lmcache-sweep" -type f | wc -l)" -eq 0 ] || { echo "!! residual files"; exit 1; }

  echo; echo "### 2. start container (L1=${L1_GB}GB, cuFile pool=${CUFILE_BUF_MIB}MiB)"
  remove_owned_container "$CTNR" || exit 1
  cat > "$EV/docker-run-cmdline.txt" <<CMD
docker run -d --name $CTNR --label kvprobe.owner=$PROBE_OWNER --gpus "\"device=${PROBE_GPU_INDEX}\"" --ipc host --cap-add IPC_LOCK --ulimit memlock=-1 $NET_MODE
  $(for d in /dev/nvidia-fs*; do printf -- '--device %s ' "$d"; done)
  --volume /run/udev:/run/udev:ro --volume $GDS_MOUNT:/gds:rw
  --volume /etc/cufile.json:/etc/cufile.json:ro --volume $MODEL_MOUNT:/models:ro
  --env LMCACHE_GDS_PATH=/gds/lmcache-sweep
  --env LMCACHE_CUFILE_BUFFER_SIZE=$CUFILE_BUF_MIB   (MiB)
  --env LMCACHE_MAX_LOCAL_CPU_SIZE=$L1_GB            (GB) — must exceed the working set or the store is truncated
  --env LMCACHE_CHUNK_SIZE=256 LMCACHE_LOCAL_CPU=True LMCACHE_SAVE_UNFULL_CHUNK=False
  vllm serve $MODEL --host $BIND --port $PORT --gpu-memory-utilization 0.80 --no-enable-prefix-caching
    --kv-transfer-config {"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}
CMD
  docker run -d --name "$CTNR" --label "kvprobe.owner=$PROBE_OWNER" \
    --gpus "\"device=${PROBE_GPU_INDEX}\"" --ipc host --cap-add IPC_LOCK --ulimit memlock=-1 \
    "${NET_ARGS[@]}" "${DEV_ARGS[@]}" \
    --volume /run/udev:/run/udev:ro \
    --volume "$GDS_MOUNT":/gds:rw \
    --volume /etc/cufile.json:/etc/cufile.json:ro \
    --volume "$MODEL_MOUNT":/models:ro \
    --env CUFILE_ENV_PATH_JSON=/etc/cufile.json \
    --env LMCACHE_GDS_PATH=/gds/lmcache-sweep \
    --env LMCACHE_CUFILE_BUFFER_SIZE="$CUFILE_BUF_MIB" \
    --env LMCACHE_CHUNK_SIZE=256 \
    --env LMCACHE_LOCAL_CPU=True \
    --env LMCACHE_MAX_LOCAL_CPU_SIZE="$L1_GB" \
    --env LMCACHE_MAX_LOCAL_DISK_SIZE=512 \
    --env LMCACHE_SAVE_UNFULL_CHUNK=False \
    --env PYTHONHASHSEED=0 \
    --env LMCACHE_LOG_LEVEL="${LOGLEVEL:-INFO}" \
    "$IMAGE" \
    vllm serve "$MODEL" --host "$BIND" --port "$PORT" \
      --served-model-name "$PROBE_MODEL" \
      --gpu-memory-utilization 0.80 --no-enable-prefix-caching \
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
    >/dev/null || { echo "!! docker run failed"; exit 1; }

  echo -n "  waiting for model load"
  for i in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1 && { echo " -> ready ($((i*5))s)"; break; }
    docker ps --format '{{.Names}}' | grep -qx "$CTNR" || { echo; docker logs --tail 30 "$CTNR"; exit 1; }
    echo -n "."; sleep 5
  done
  curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null || { echo "!! startup timeout"; exit 1; }

  echo; echo "### 3. backend check"
  if docker logs "$CTNR" 2>&1 | grep -q "Failed during post_init"; then
    echo "!! init failed — degraded mode. Retry with LOGLEVEL=DEBUG."
    docker logs "$CTNR" 2>&1 | grep -iE "post_init|init failed" | tail -5; exit 1
  fi
  docker logs "$CTNR" 2>&1 | grep -iE "GDS backend using fstype|Using cufile|Created backend" \
    | tee "$EV/backend-init.txt"
  grep -q GdsBackend "$EV/backend-init.txt" || { echo "!! no GdsBackend"; exit 1; }

  echo; echo "### 4. GATE — is the max context actually stored natively"
  W0=$(writes)
  PORT="$PORT" MODEL_NAME="$(basename "$MODEL")" python3 - <<'PROBE'
import json, subprocess, tempfile, os
S = (
    "LMCache reuses previously computed key-value cache blocks when the same long "
    "context appears again. This deterministic sentence exists only to control "
    "the token length of this storage experiment. "
)
# Keep this setup probe well below the 32,768-token window while producing
# several complete chunks. The measurement client sizes measured payloads with
# /tokenize; this setup probe only establishes a native device write.
txt = S * 100 + "\n\nDo not summarize the context. Reply with exactly 'STAGE1_D6_OK'."
b = {"model": os.environ.get("MODEL_NAME", "Qwen2.5-32B-Instruct"), "messages": [{"role": "user", "content": txt}],
     "max_tokens": 7, "temperature": 0}
# a long body exceeds the command-line argument limit (128KB), so pass it as a file
f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
json.dump(b, f); f.close()
r = subprocess.run(["curl", "-s", "--max-time", "600", "-X", "POST",
                    f"http://127.0.0.1:{os.environ.get('PORT', '8102')}/v1/chat/completions",
                    "-H", "Content-Type: application/json", "--data-binary", "@" + f.name],
                   capture_output=True, text=True)
os.unlink(f.name)
print("probe response:", r.stdout[:160] if r.stdout else f"(none) stderr={r.stderr[:160]}")
PROBE
  sleep 3; W1=$(writes)
  BIG=$(find "$GDS_MOUNT/lmcache-sweep" -type f -size +64k | wc -l)
  BYTES=$(du -sb "$GDS_MOUNT/lmcache-sweep" | cut -f1)
  ERRS=$(docker logs "$CTNR" 2>&1 | grep -c "cuFileHandleRegister failed")
  STORED=$(docker logs "$CTNR" 2>&1 | grep -oE "Stored [0-9]+ out of total [0-9]+" | tail -1)
  { echo "nvfs_writes_delta=$((W1-W0))"; echo "register_failed=$ERRS";
    echo "files_over_64k=$BIG"; echo "bytes=$BYTES"; echo "stored=$STORED"; } | tee "$EV/gate.txt"
  echo
  if [ "$((W1-W0))" -gt 0 ] && [ "$ERRS" -eq 0 ] && [ "$BIG" -gt 0 ]; then
    echo "gate_pass=1" | tee -a "$EV/gate.txt"
    echo "OK gate passed — next: bash $0 measure"
    echo "   (if the two 'stored' numbers differ, L1/pool is too small and the store was truncated — raise L1_GB)"
  else
    echo "gate_pass=0" | tee -a "$EV/gate.txt"
    echo "FAIL gate — do not measure"
    exit 1
  fi
  ;;

measure)
  grep -qx 'gate_pass=1' "$EV/gate.txt" 2>/dev/null \
    || { echo "!! native GDS write gate has not passed; run: bash $0 setup"; exit 1; }
  W0=$(writes)
  RUN_DIR=$EV/run GDS_PORT=$PORT GDS_CTNR=$CTNR RECO_PORT=$RECO_PORT RECO_CTNR=$RECO_CTNR \
  N="${N:-10}" FILLERS="${FILLERS:-6}" FILLER_TOK="${FILLER_TOK:-8192}" \
  CONTEXTS="${CONTEXTS:-512,1024,2048,4096,5632,8192}" \
    python3 "$CLIENT" 2>&1 | tee "$EV/measure-console.txt"
  echo "nvfs_writes: $W0 -> $(writes)" | tee "$EV/measure-counter.txt"
  ;;

collect)
  cp "$CLIENT" "$0" "$EV"/ 2>/dev/null
  docker inspect "$CTNR" --format '{{json .Config.Env}}' > "$EV/container-env.json" 2>&1
  docker logs "$CTNR" 2>&1 | grep -iE "Retrieved|Stored .* tokens" > "$EV/lmcache-io-lines.txt"
  docker logs "$CTNR" > "$EV/container-full.log" 2>&1; gzip -9f "$EV/container-full.log"
  { date -u; uname -a; nvidia-smi --query-gpu=index,name,pci.bus_id --format=csv; echo "measured_device_id=$(nvidia-smi -i "$PROBE_GPU_INDEX" --query-gpu=pci.bus_id --format=csv,noheader | tr -d ' ')"; echo "measured_gpu_index=$PROBE_GPU_INDEX";
    findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS "$GDS_MOUNT"; } > "$EV/provenance.txt"
  du -sb "$GDS_MOUNT/lmcache-sweep" > "$EV/gds-path-usage.txt"
  ( cd "$EV" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | sort | xargs sha256sum > SHA256SUMS )
  BUNDLE="$HOME/$(basename "$EV").tar.gz"
  tar czf "$BUNDLE" -C "$(dirname "$EV")" "$(basename "$EV")"
  echo "bundle: $BUNDLE ($(du -h "$BUNDLE"|cut -f1), $(wc -l < "$EV/SHA256SUMS") files)"
  ;;

recompute0)
  # Re-measure the sweep's recompute arm on the same GPU as the GDS arm.
  # The GDS arm data is already captured, so the two containers need not run at once.
  sudo -v || exit 1
  R0=vllm-recompute-gpu0; RP=8103
  MODEL_MOUNT=$(docker inspect "$RECO_CTNR" \
    --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)
  [ -n "$MODEL_MOUNT" ] || { echo "!! could not find the /models mount"; exit 1; }
  NET_MODE=$(docker inspect "$RECO_CTNR" --format '{{.HostConfig.NetworkMode}}')
  case "$NET_MODE" in
    host) NET_ARGS=(--network host); BIND=$SERVE_HOST ;;
    *) NET_ARGS=(--publish "$SERVE_HOST:$RP:$RP"); BIND=0.0.0.0 ;;
  esac

  echo "### Free GPU${PROBE_GPU_INDEX} — stop the GDS sweep container (measurement data already collected)"
  if docker inspect "$CTNR" >/dev/null 2>&1; then
    remove_owned_container "$CTNR" || exit 1
    echo "  removed $CTNR"
  fi
  remove_owned_container "$R0" || exit 1
  GPU_USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$PROBE_GPU_INDEX" | tr -d ' ')
  echo "  GPU${PROBE_GPU_INDEX} in use: ${GPU_USED} MiB"
  [ "${GPU_USED:-0}" -gt 5000 ] && { echo "!! GPU${PROBE_GPU_INDEX} occupied"; docker ps --format '  {{.Names}}'; exit 1; }

  echo "### start recompute container (GPU${PROBE_GPU_INDEX}, no KV connector)"
  docker run -d --name "$R0" --label "kvprobe.owner=$PROBE_OWNER" --gpus "\"device=${PROBE_GPU_INDEX}\"" --ipc host "${NET_ARGS[@]}" \
    --volume "$MODEL_MOUNT":/models:ro "$IMAGE" \
    vllm serve "$MODEL" --host "$BIND" --port "$RP" \
      --served-model-name "$PROBE_MODEL" \
      --gpu-memory-utilization 0.80 --no-enable-prefix-caching >/dev/null \
    || { echo "!! docker run failed"; exit 1; }
  echo -n "  waiting for model load"
  for i in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$RP/v1/models" >/dev/null 2>&1 && { echo " -> ready ($((i*5))s)"; break; }
    docker ps --format '{{.Names}}' | grep -qx "$R0" || { echo; docker logs --tail 30 "$R0"; exit 1; }
    echo -n "."; sleep 5
  done
  curl -sf "http://127.0.0.1:$RP/v1/models" >/dev/null || { echo "!! startup timeout"; exit 1; }

  echo; echo "### measure recompute arm (GPU${PROBE_GPU_INDEX}) — payload rebuilt identically to the sweep"
  RUN_DIR=$EV/run-recompute-gpu0 GDS_PORT=$RP TOKENIZE_PORT=$RP \
  RECO_PORT=$RP RECO_CTNR=$R0 SKIP_GDS=1 N="${N:-10}" \
  CONTEXTS="${CONTEXTS:-256,512,1024,2048,4096}" \
    python3 "$CLIENT" 2>&1 | tee "$EV/recompute0-console.txt"
  echo
  echo "note: record these prompt counts as transport/english-v1; do not compare them with the former localized workload"
  ;;

cleanup) remove_owned_container "$CTNR" && echo "$CTNR removed" ;;
*) echo "usage: bash $0 {setup|measure|collect|recompute0|cleanup}"; exit 1 ;;
esac
