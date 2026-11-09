#!/usr/bin/env bash
# LMCache v0.4.1 NIXL static/GDS plug-in matched arm.
# Usage: bash nixl_matched.sh {preflight|setup|measure|matched|collect|cleanup}
#
# Question: over the SAME device and cuFile, does the NIXL abstraction
# change the tier vs the native LMCache GdsBackend? This arm changes the L2 stack
# (LMCache NIXL static storage -> NIXL GDS/GDS_MT plug-in -> cuFile) and reuses the same client,
# payload, eviction and kernel-counter attribution as the native GDS arm. If only GDS_MT
# reaches the device, the comparison also includes single- vs multi-threaded cuFile behavior.
#
# ⚠️ EXPERIMENTAL: never run against live arms; requires the `nixl` package and a
# GDS-capable NIXL plug-in inside the image (see `preflight`). Config verified against LMCache
# v0.4.1 source (nixl_storage_backend.py / config.py / cache_engine.py). Compare its output to
# the native GDS arm; do not name a winner until gate 1 (per-request device-read attribution)
# passes for both.
set -uo pipefail
# Which GPU this run measures. Storage counters do not identify the accelerator
# that issued I/O: nvidia-fs is host-wide, while cgroup accounting is filtered
# to the recorded tier block device. Record device_id (and gpu_index if stated)
# in the configuration runtime block as well.
export PROBE_GPU_INDEX="${PROBE_GPU_INDEX:-0}"

# Required — name your image and GDS-capable mount (no defaults).
IMAGE=${IMAGE:?set IMAGE to the vLLM+LMCache container image built for this host}
GDS_MOUNT=${GDS_MOUNT:?set GDS_MOUNT to the GDS-capable filesystem mount on this host}
# Host path of the tier filesystem, for the client's per-device cgroup counter. Without
# it the NIXL arm records no tier device and its Gate 1 can never be more than UNPROVEN.
export PROBE_TIER_MOUNT="${PROBE_TIER_MOUNT:-$GDS_MOUNT}"

MODEL=${MODEL:-/models/Qwen2.5-32B-Instruct}
# Served name / request "model" field default to the model directory basename so one
# MODEL override reparameterizes both. (NIXL knobs below are unaffected.)
export PROBE_MODEL="${PROBE_MODEL:-$(basename "$MODEL")}"
CTNR=${CTNR:-kv-nixl}
PORT=${PORT:-8106}
# The serving API has no authentication, so it listens on loopback only. SERVE_HOST opens it
# to other interfaces — set it only on a network you control.
SERVE_HOST=${SERVE_HOST:-127.0.0.1}
# The measurement client: next to this script when copied flat, else in the parent scripts/
# directory of a checkout — never a fixed path in /tmp that another user could write first.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ -z "${CLIENT:-}" ]]; then
  CLIENT=$HERE/g5_controlled_kv_experiment.py
  [[ -f "$CLIENT" ]] || CLIENT=$HERE/../g5_controlled_kv_experiment.py
fi
GDSCHECK=${GDSCHECK:-/usr/local/cuda-13.3/gds/tools/gdscheck}
# Optional host-side diagnostic override copied from the image and patched separately (v0.4.1-only).
# When set, setup bind-mounts it over LMCache's installed NIXL backend module. See
# scripts/tools/nixl_v0.4.1_background_transfer_diag.patch. Empty by default (no effect).
NIXL_DIAG_BACKEND_PY=${NIXL_DIAG_BACKEND_PY:-}
# Optional destination override for unusual images. When the diagnostic is enabled and this is
# empty, setup discovers the installed module path from the image without importing the module.
NIXL_BACKEND_CONTAINER_PATH=${NIXL_BACKEND_CONTAINER_PATH:-}
# Optional diagnostic library path. Leave empty for the image's normal loader path.
NIXL_LD_LIBRARY_PATH=${NIXL_LD_LIBRARY_PATH:-}
# NIXL config (LMCache v0.4.1). buffer size is BYTES; device=cuda for VRAM-direct GDS.
NIXL_BACKEND=${NIXL_BACKEND:-GDS_MT}              # GDS_MT is primary; GDS is diagnostic
NIXL_BACKEND_LOWER=$(printf '%s' "$NIXL_BACKEND" | tr '[:upper:]' '[:lower:]')
NIXL_BACKEND_ID="lmcache_nixl_static_${NIXL_BACKEND_LOWER}"
NIXL_BUFFER_BYTES=${NIXL_BUFFER_BYTES:-3221225472} # 3 GiB (comparable to native cuFile pool 3072 MiB)
NIXL_POOL_SIZE=${NIXL_POOL_SIZE:-512}             # >= total chunks held; each = one 256-token chunk
L1_GB=${L1_GB:-3}
CONTEXTS=${CONTEXTS:-4096}
EV=${EVIDENCE:-/tmp/nixl-evidence}

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

# Host directory mounted at /models. MODEL_HOST_DIR names it; without it, it is read from
# the /models mount of an existing arm.
model_mount() {
  if [ -n "${MODEL_HOST_DIR:-}" ]; then
    [ -d "$MODEL_HOST_DIR" ] || { echo "!! MODEL_HOST_DIR is not a directory: $MODEL_HOST_DIR" >&2; return 1; }
    printf '%s\n' "$MODEL_HOST_DIR"; return 0
  fi
  for src in vllm-baseline vllm-lmcache-sweep kv-controlled; do
    v=$(docker inspect "$src" --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)
    [ -n "$v" ] && { printf '%s\n' "$v"; return 0; }
  done
  return 1
}

case "${1:-}" in

preflight)
  echo "### NIXL availability inside the image (primary risk) ###"
  docker run --rm "$IMAGE" python3 -c "import nixl._api; print('  nixl._api import OK')" \
    || { echo "!! nixl package not importable in image — this arm cannot run (rebuild image with NIXL)"; exit 1; }
  echo "  (setup confirms whether the GDS/GDS_MT plug-in can actually be instantiated)"
  echo "### GDS prerequisites (same as native GDS) ###"
  ls /dev/nvidia-fs* >/dev/null 2>&1 && echo "  nvidia-fs devices OK" || { echo "!! no /dev/nvidia-fs*"; exit 1; }
  [ -r /etc/cufile.json ] && echo "  /etc/cufile.json OK" \
    || { echo "!! /etc/cufile.json not readable"; exit 1; }
  GDS_OUT=$( { command -v gdscheck >/dev/null 2>&1 && gdscheck -p; } 2>/dev/null || "$GDSCHECK" -p 2>/dev/null )
  echo "$GDS_OUT" | grep -i 'use_compat_mode' | sed 's/^/  gdscheck: /' || echo "  (gdscheck: use_compat_mode line not found — check GDSCHECK path or PATH)"
  echo "$GDS_OUT" | grep -qiE 'use_compat_mode[[:space:]]*:[[:space:]]*false' \
    && echo "  use_compat_mode:false OK" \
    || { echo "!! use_compat_mode:false not confirmed — refusing a native-GDS run"; exit 1; }
  ;;

setup)
  sudo -v || exit 1
  : > "$EV/gate.txt"
  DEV_ARGS=(); for d in /dev/nvidia-fs*; do [ -e "$d" ] && DEV_ARGS+=(--device "$d"); done
  [ ${#DEV_ARGS[@]} -eq 0 ] && { echo "!! no /dev/nvidia-fs*"; exit 1; }
  DIAG_MOUNT_ARGS=()
  if [ -n "$NIXL_DIAG_BACKEND_PY" ]; then
    [ -f "$NIXL_DIAG_BACKEND_PY" ] || { echo "!! NIXL_DIAG_BACKEND_PY not found: $NIXL_DIAG_BACKEND_PY"; exit 1; }
    if [ -z "$NIXL_BACKEND_CONTAINER_PATH" ]; then
      NIXL_BACKEND_CONTAINER_PATH=$(docker run --rm "$IMAGE" python3 -c \
        "import importlib.util; print(importlib.util.find_spec('lmcache.v1.storage_backend.nixl_storage_backend').origin)")
    fi
    [ -n "$NIXL_BACKEND_CONTAINER_PATH" ] \
      || { echo "!! could not discover the installed NIXL backend module path"; exit 1; }
    DIAG_SOURCE=$(cd "$(dirname "$NIXL_DIAG_BACKEND_PY")" && pwd)/$(basename "$NIXL_DIAG_BACKEND_PY")
    DIAG_MOUNT_ARGS+=(--volume "$DIAG_SOURCE:$NIXL_BACKEND_CONTAINER_PATH:ro")
    echo "  NIXL diagnostic override: $DIAG_SOURCE -> $NIXL_BACKEND_CONTAINER_PATH"
  fi
  LIBRARY_ENV_ARGS=()
  if [ -n "$NIXL_LD_LIBRARY_PATH" ]; then
    LIBRARY_ENV_ARGS+=(--env "LD_LIBRARY_PATH=$NIXL_LD_LIBRARY_PATH")
    echo "  NIXL diagnostic LD_LIBRARY_PATH: $NIXL_LD_LIBRARY_PATH"
  fi
  MODEL_MOUNT=$(model_mount) || { echo "!! could not find a running arm with /models mounted"; exit 1; }
  echo "  model host dir: $MODEL_MOUNT"

  echo "### clean NIXL gds path"
  reset_tier_dir "$GDS_MOUNT" lmcache-nixl || exit 1

  echo "### free GPU${PROBE_GPU_INDEX} + start NIXL-GDS container (backend=$NIXL_BACKEND, buffer=${NIXL_BUFFER_BYTES}B, pool=$NIXL_POOL_SIZE)"
  # Remove this arm and other known single-GPU experiment arms on the target GPU.
  # vllm-baseline (recompute arm, GPU1 + model-mount source) is intentionally kept.
  for old in "$CTNR" vllm-lmcache-sweep kv-controlled vllm-recompute-gpu0 vllm-lmcache-gds; do
    if docker inspect "$old" >/dev/null 2>&1; then
      remove_owned_container "$old" || exit 1
      echo "  removed experiment container $old (re-run its own setup to restore)"
    fi
  done
  GPU_USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$PROBE_GPU_INDEX" | tr -d ' ')
  [ "${GPU_USED:-0}" -gt 5000 ] && { echo "!! GPU${PROBE_GPU_INDEX} still in use (${GPU_USED} MiB) — stop the occupant manually"; docker ps --format '  {{.Names}}'; exit 1; }

  EXTRA=$(printf '{"enable_nixl_storage": true, "nixl_backend": "%s", "nixl_pool_size": %s, "nixl_path": "/gds/lmcache-nixl", "use_direct_io": true}' "$NIXL_BACKEND" "$NIXL_POOL_SIZE")
  docker run -d --name "$CTNR" --label "kvprobe.owner=$PROBE_OWNER" \
    --gpus "\"device=${PROBE_GPU_INDEX}\"" --ipc host --cap-add IPC_LOCK --ulimit memlock=-1 --network host \
    "${DEV_ARGS[@]}" \
    --volume /run/udev:/run/udev:ro \
    --volume "$GDS_MOUNT":/gds:rw \
    --volume /etc/cufile.json:/etc/cufile.json:ro \
    --volume "$MODEL_MOUNT":/models:ro \
    "${DIAG_MOUNT_ARGS[@]}" \
    --env CUFILE_ENV_PATH_JSON=/etc/cufile.json \
    --env LMCACHE_CHUNK_SIZE=256 \
    --env LMCACHE_LOCAL_CPU=True \
    --env LMCACHE_MAX_LOCAL_CPU_SIZE="$L1_GB" \
    --env LMCACHE_SAVE_UNFULL_CHUNK=False \
    --env LMCACHE_NIXL_BUFFER_SIZE="$NIXL_BUFFER_BYTES" \
    --env LMCACHE_NIXL_BUFFER_DEVICE=cuda \
    --env LMCACHE_EXTRA_CONFIG="$EXTRA" \
    --env PYTHONHASHSEED=0 \
    --env LMCACHE_LOG_LEVEL="${LOGLEVEL:-INFO}" \
    "${LIBRARY_ENV_ARGS[@]}" \
    "$IMAGE" \
    vllm serve "$MODEL" --host "$SERVE_HOST" --port "$PORT" \
      --served-model-name "$PROBE_MODEL" \
      --gpu-memory-utilization 0.80 --no-enable-prefix-caching \
      --kv-transfer-config '{"kv_connector":"LMCacheConnectorV1","kv_role":"kv_both"}' \
    >/dev/null || { echo "!! docker run failed"; exit 1; }
  {
    echo "  backend=$NIXL_BACKEND_ID"
    echo "  storage_mode=static"
    echo "  LMCACHE_EXTRA_CONFIG=$EXTRA"
  } | tee "$EV/nixl-config.txt"

  echo -n "  waiting for model load"
  for i in $(seq 1 80); do
    curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1 && { echo " -> ready ($((i*5))s)"; break; }
    docker ps --format '{{.Names}}' | grep -qx "$CTNR" || { echo; docker logs --tail 40 "$CTNR"; exit 1; }
    echo -n "."; sleep 5
  done
  curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null || { echo "!! startup timeout"; exit 1; }

  echo "### backend init check — NIXL storage must be created (not degraded to recompute)"
  if docker logs "$CTNR" 2>&1 | grep -q "Failed during post_init"; then
    echo "!! init failed — degraded mode. Retry with LOGLEVEL=DEBUG."; docker logs "$CTNR" 2>&1 | grep -iE "post_init|nixl|error" | tail -8; exit 1
  fi
  docker logs "$CTNR" 2>&1 | grep -iE "nixl|storage backend|Created backend|GDS|error=5027|file register" | tee "$EV/backend-init.txt" | tail -12
  echo "   (the startup log must show creation of the NIXL storage backend)"
  if docker logs "$CTNR" 2>&1 | grep -qiE "error=5027|file register error"; then
    echo "❌ GDS file-register error=5027 in logs — NIXL GDS fast path fell back (POSIX), not on the device."
    echo "   This state is not a native-GDS tier and cannot be measured as one."
  fi

  echo "### GATE — native GDS write via NIXL, silent compat/POSIX fallback, or no write?"
  # NixlFilePool pre-creates pool_size files (obj_*.bin) at init. A store fans out write-through
  # to every backend (storage_manager.batched_put), so the NIXL backend receives every chunk and
  # writes page_size bytes per chunk into a pool file. The write is async (batched_submit_put_task
  # schedules mem_to_storage on a background loop without awaiting). Two independent signals:
  #   - nvidia-fs Writes counter  -> increments ONLY on the native cuFile (GDS) path
  #   - bytes-on-device in pool files -> increases whether the path is native GDS OR compat/POSIX
  # so (writes>0)=native; (writes==0 & bytes>0)=silent compat fallback; (both flat)=no write.
  POOL_DIR="$GDS_MOUNT/lmcache-nixl"
  POOL_N0=$(ls "$POOL_DIR"/obj_*.bin 2>/dev/null | wc -l)
  POOL_BYTES0=$(find "$POOL_DIR" -name 'obj_*.bin' -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
  echo "  pool files pre-created at init: $POOL_N0 (expected ~$NIXL_POOL_SIZE); bytes-on-device now: $POOL_BYTES0"
  W0=$(writes)
  PORT="$PORT" PROBE_MODEL="$PROBE_MODEL" python3 - <<'PROBE'
import json, subprocess, tempfile, os
S = (
    "LMCache reuses previously computed key-value cache blocks when the same long "
    "context appears again. This deterministic sentence exists only to control "
    "the token length of this storage experiment. "
)
# This setup probe stays well below the context window and stores several chunks.
txt = S * 100 + "\n\nDo not summarize the context. Reply with exactly 'STAGE1_D6_OK'."
b = {"model": os.environ.get("PROBE_MODEL", "Qwen2.5-32B-Instruct"), "messages": [{"role": "user", "content": txt}], "max_tokens": 7, "temperature": 0}
f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8"); json.dump(b, f); f.close()
r = subprocess.run(["curl","-s","--max-time","600","-X","POST",f"http://127.0.0.1:{os.environ.get('PORT','8106')}/v1/chat/completions",
                    "-H","Content-Type: application/json","--data-binary","@"+f.name], capture_output=True, text=True)
os.unlink(f.name); print("probe response:", r.stdout[:160] if r.stdout else f"(none) {r.stderr[:160]}")
PROBE
  # NIXL put is async (fire-and-forget on a background loop); poll up to 30s for the device write.
  W1=$W0
  for i in $(seq 1 15); do sleep 2; W1=$(writes); [ "$((W1-W0))" -gt 0 ] && break; done
  POOL_BYTES1=$(find "$POOL_DIR" -name 'obj_*.bin' -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')
  POOL_NONZERO=$(find "$POOL_DIR" -name 'obj_*.bin' -size +0c 2>/dev/null | wc -l)
  POOL_MAX=$(find "$POOL_DIR" -name 'obj_*.bin' -printf '%s\n' 2>/dev/null | sort -n | tail -1)
  REG5027=$(docker logs "$CTNR" 2>&1 | grep -c -iE "error=5027|file register error")
  {
    echo "nvfs_writes_delta=$((W1-W0))"
    echo "pool_bytes_delta=$((POOL_BYTES1-POOL_BYTES0))"
    echo "pool_nonzero_files=$POOL_NONZERO"
    echo "pool_max_file_bytes=${POOL_MAX:-0}"
    echo "gds_register_error_5027=$REG5027"
  } | tee "$EV/gate.txt"
  if [ "$((W1-W0))" -gt 0 ] && [ "$REG5027" -eq 0 ]; then
    echo "gate_pass=1" | tee -a "$EV/gate.txt"
    echo "✅ gate PASS — native GDS write via NIXL observed on nvidia-fs (no 5027); next: bash $0 measure"
  elif [ "$((POOL_BYTES1-POOL_BYTES0))" -gt 0 ] || [ "$POOL_NONZERO" -gt 0 ]; then
    echo "gate_pass=0" | tee -a "$EV/gate.txt"
    echo "⚠️  gate FAIL (SILENT FALLBACK) — bytes landed in the NIXL pool files, but nvidia-fs Writes did NOT increase."
    echo "   cuFile fell back to compat/POSIX for these files (no 5027 raised). Data is on disk, NOT on the native GDS tier."
    echo "   This is a measurable matched-path finding: over the SAME mount, NIXL GDS did not take the native cuFile path that"
    echo "   LMCache's own GdsBackend uses. Do NOT measure this as a native-GDS arm; record the fallback."
    echo "   Diagnose: docker exec $CTNR sh -c 'cat /var/log/cufile.log 2>/dev/null | tail -20'; gdscheck -p | grep -i compat"
    exit 1
  else
    echo "gate_pass=0" | tee -a "$EV/gate.txt"
    echo "❌ gate FAIL (NO WRITE) — neither nvidia-fs Writes nor pool bytes changed after 30s."
    echo "   The async put may not have run or errored silently. Check: docker logs $CTNR 2>&1 | grep -iE 'nixl|error|transfer|post_init'"
    exit 1
  fi
  ;;

measure)
  sudo -v || exit 1
  grep -qx 'gate_pass=1' "$EV/gate.txt" 2>/dev/null \
    || { echo "!! NIXL native-write gate has not passed; run: bash $0 setup"; exit 1; }
  [ -f "$CLIENT" ] || { echo "!! copy g5_controlled_kv_experiment.py to $CLIENT"; exit 1; }
  curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null || { echo "!! NIXL arm not up on $PORT (run: bash $0 setup)"; exit 1; }
  # Reuse the native-GDS client path: attribution for a NIXL GDS read is the same nvidia-fs
  # counter, so --arm gds applies. Output is labelled nixl for comparison with the native arm.
  # shellcheck disable=SC2086
  OUTPUT=${OUTPUT:-$EV/nixl-context.json}
  MEASURE_CONSOLE=${MEASURE_CONSOLE:-$EV/measure-console.txt}
  python3 "$CLIENT" context --arm gds --port "$PORT" --container "$CTNR" \
    --backend "$NIXL_BACKEND_ID" \
    --output "$OUTPUT" --contexts $CONTEXTS \
    --samples "${N:-10}" --warmup "${WARMUP:-2}" --fillers "${FILLERS:-6}" --filler-tokens "${FILLER_TOKENS:-8192}" 2>&1 | tee "$MEASURE_CONSOLE"
  echo "→ compare $OUTPUT TTFT/tier gates against the native GDS arm at the same context."
  ;;

matched)
  for run in 1 2 3; do
    OUTPUT="$EV/nixl-run${run}.json" \
      MEASURE_CONSOLE="$EV/nixl-run${run}-console.txt" \
      N=10 WARMUP=2 bash "$0" measure
  done
  ;;

collect)
  cp "$CLIENT" "$EV"/ 2>/dev/null
  docker inspect "$CTNR" --format '{{json .Config.Env}}' > "$EV/container-env.json" 2>&1
  docker logs "$CTNR" > "$EV/container-full.log" 2>&1; gzip -9f "$EV/container-full.log"
  { date -u; uname -a; nvidia-smi --query-gpu=index,name,pci.bus_id --format=csv; echo "measured_device_id=$(nvidia-smi -i "$PROBE_GPU_INDEX" --query-gpu=pci.bus_id --format=csv,noheader | tr -d ' ')"; echo "measured_gpu_index=$PROBE_GPU_INDEX"; } > "$EV/provenance.txt"
  ( cd "$EV" && find . -type f ! -name SHA256SUMS | sed 's|^\./||' | sort | xargs sha256sum > SHA256SUMS )
  BUNDLE="$HOME/$(basename "$EV").tar.gz"
  tar czf "$BUNDLE" -C "$(dirname "$EV")" "$(basename "$EV")"
  echo "bundle: $BUNDLE ($(wc -l < "$EV/SHA256SUMS") files)"
  ;;

cleanup) remove_owned_container "$CTNR" && echo "$CTNR removed" ;;
*) echo "usage: bash $0 {preflight|setup|measure|matched|collect|cleanup}"; exit 1 ;;
esac
