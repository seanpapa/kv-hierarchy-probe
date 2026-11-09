# LMCache disk storage layouts

> Last checked: 2026-09-29  
> Scope: three concrete LMCache paths examined with 256-token chunks: buffered POSIX,
> native `GdsBackend`, and LMCache NIXL static storage with the NIXL `GDS_MT` plug-in.
> These layouts are implementation- and version-specific, not universal properties of POSIX,
> GPUDirect Storage, or NIXL.

All three paths can store the same logical KV chunk while using different identity, metadata,
lookup, and space-reuse mechanisms.

## At a glance

| Property | Buffered POSIX | Native cuFile `GdsBackend` | LMCache NIXL static + `GDS_MT` |
|---|---|---|---|
| Logical cache unit | Keyed chunk | Keyed chunk | Keyed chunk |
| Backing layout | One key-derived `.pt` file | Key-derived data file + metadata sidecar | Bounded pool of slot files |
| Key to location | In-memory metadata + deterministic path | Hot index + sharded key path | In-memory `key_dict` to slot/FD |
| Payload destination | Host staging buffer | Registered GPU buffer | Registered GPU buffer |
| Payload path | Buffered POSIX/page cache | cuFile GDS | NIXL `GDS_MT` to cuFile |
| Eviction | Remove key and file | Remove key and data/sidecar pair | Remove mapping and return slot; retain file |
| Restart recovery in examined implementation | No automatic rebuild | Sidecar scan rebuilds index | No automatic key-to-slot rebuild |

## Buffered POSIX: one key-derived payload file

```text
CacheEngineKey
  -> deterministic path ending in .pt
  -> raw KV bytes
  -> buffered file I/O through the Linux page cache
```

Despite the `.pt` suffix, the examined backend writes raw bytes rather than a general
`torch.save()` archive. Runtime metadata such as shape and dtype is held in process memory.

On retrieval, the backend allocates or obtains a host buffer, reads the file into that buffer,
and the serving integration completes the transfer toward the active KV cache. A fast repeat read
can therefore be a page-cache hit rather than storage service.

**Measurement consequence.** A file's existence and correct output do not prove a device read.
The controlled interval needs block/cgroup device-byte evidence, and the experiment must make the
target pages cold. `cgroup io.stat` reports block-I/O bytes for the cgroup; it is not a per-request
hardware trace and must be isolated from unrelated I/O.

**Source-confirmed.** The examined `LocalDiskBackend` uses a key-derived path, raw-byte file
helpers, process-local metadata, and file deletion on removal.

## Native cuFile GDS: data and sidecar as a pair

```text
sharded key path/
  <encoded-key>.kvcache.safetensors
  <encoded-key>.kvcache.safetensors.metadata
```

The examined data file has a small header followed by the KV payload. The sidecar stores the
metadata needed to reconstruct the cache entry and rebuild the backend's hot index at startup.
Metadata is handled with ordinary file I/O; the payload is transferred with cuFile.

The two files have different jobs:

- the data header describes the payload file itself;
- the sidecar is cheap to enumerate and read while rebuilding the key/index view;
- a cache hit transfers the payload, not the sidecar, to GPU memory.

The directory is sharded from a hash prefix to avoid concentrating all key-derived files in one
leaf directory. The directory prefix is a placement aid; the full encoded key remains the file
identity.

**Source-derived example.** The examined `GdsBackend` packs the metadata block to exactly 4 KiB
([`_METADATA_MAX_SIZE`](https://github.com/LMCache/LMCache/blob/1d8716508371ab5c95de0d963160e1e64de467ef/lmcache/v1/storage_backend/gds_backend.py#L117-L122)) and starts the payload at
that offset ([L902](https://github.com/LMCache/LMCache/blob/1d8716508371ab5c95de0d963160e1e64de467ef/lmcache/v1/storage_backend/gds_backend.py#L902)). A 64 MiB chunk's data file is
therefore 4,096 + 67,108,864 = 67,112,960 bytes, and its sidecar is the same 4 KiB block. These sizes
follow that implementation and chunk size; they are not GDS constants.

**Measurement consequence.** `nvidia-fs` payload-byte deltas can corroborate GDS activity, while
the small metadata operations remain a separate POSIX path. A cuFile logical operation is not a
promise about physical NVMe command size.

## LMCache NIXL static storage: key-to-slot mapping

```text
startup
  -> create/open P backing files
  -> retain P file descriptors
  -> register FILE descriptors with NIXL

store
  CacheEngineKey -> free slot index -> registered FD -> NIXL transfer

evict
  remove key mapping -> return slot -> keep backing file
```

Names such as `obj_97_c8b6.bin` identify a backing resource created for the pool. The numeric
label and process-random suffix are not the KV chunk hash. The authoritative runtime relation is
the LMCache key-to-slot mapping.

The file size also does not prove current occupancy. An unwritten slot can be zero length; a slot
written previously can retain its payload length after eviction because eviction returns the slot
without truncating the backing file. The next admission overwrites the reused slot.

LMCache owns the pool, slot allocation, lookup, pinning/eviction policy, and key mapping. The NIXL
agent registers descriptors and creates transfer handles; `GDS_MT` executes the descriptor's
FILE-to-memory or memory-to-FILE byte operation through cuFile.

**Observed example.** A controlled run configured 512 pool files (`nixl_pool_size=512`,
[`../NIXL-STATIC-GDS-MATCHED.md`](../NIXL-STATIC-GDS-MATCHED.md)) and observed 64 MiB backing files after
use. That document's write gate checks, on a clean pool, that the pool files grew by
`chunks × chunk-bytes`. These are configuration-scoped observations, not a guarantee that all static
pools have 512 files or that file size equals live-key count.

**Restart boundary.** In the examined static backend, the key-to-slot dictionary is process-local.
Backing files alone do not reconstruct which cache key owns a slot after restart. Describe this as
a persistent cache only when a separately verified recovery contract exists.

## Same chunk, different lifecycle

```text
buffered POSIX eviction
  invalidate key -> delete one payload file

native GDS eviction
  invalidate key -> delete data and sidecar

NIXL static eviction
  invalidate key -> return slot -> retain registered backing file
```

The common logical unit is the chunk. “Space reuse” means that a later admission can use the
released capacity; it does not mean an evicted payload remains a valid cache hit.

## Claims this reference does not support

This document does not establish:

- that one backend is faster than another;
- that a file listing reveals live occupancy or current key ownership;
- that every repeat retrieval reached storage;
- crash consistency or atomic-publication guarantees;
- physical NVMe request geometry;
- restart recovery outside the exact implementations described;
- distributed sharing, ownership, or cache coherence.

For runtime proof, combine configuration identity, controlled store/retrieve intervals, correctness,
expected bytes, and path-specific counters as required by [`../METHOD.md`](../METHOD.md).

## Public sources and version trail

- [LMCache repository](https://github.com/LMCache/LMCache)
- [LMCache storage configuration reference](https://github.com/LMCache/LMCache/blob/1d8716508371ab5c95de0d963160e1e64de467ef/docs/source/api_reference/configurations.rst)
- [`LocalDiskBackend` source](https://github.com/LMCache/LMCache/blob/1d8716508371ab5c95de0d963160e1e64de467ef/lmcache/v1/storage_backend/local_disk_backend.py)
- [`GdsBackend` source](https://github.com/LMCache/LMCache/blob/1d8716508371ab5c95de0d963160e1e64de467ef/lmcache/v1/storage_backend/gds_backend.py)
- [LMCache NIXL storage source](https://github.com/LMCache/LMCache/blob/1d8716508371ab5c95de0d963160e1e64de467ef/lmcache/v1/storage_backend/nixl_storage_backend.py)
- [LMCache PR #1223: initial NIXL storage backend](https://github.com/LMCache/LMCache/pull/1223)
- [LMCache PR #2936: dynamic FILE support](https://github.com/LMCache/LMCache/pull/2936)
- [NIXL backend guide](https://github.com/ai-dynamo/nixl/blob/d5c127e558fd4ddb12e47127243057339c2c2b72/docs/BackendGuide.md) (NIXL 0.10.1)

LMCache links are pinned to `dev` at `1d87165` (checked 2026-09-29). The key paths, file suffixes,
the 4 KiB metadata reservation, and the `obj_{i}_{uuid}.bin` pool names are also present in LMCache
v0.4.1 (`dfc914c`), the version used in the observed runs.

When source paths or class names change, update this note with a commit-pinned link and keep the
old behavior explicitly version-scoped rather than silently rewriting history.
