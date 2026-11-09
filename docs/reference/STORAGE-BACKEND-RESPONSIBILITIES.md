# Storage backend responsibilities: LMCache, NIXL, and KVBM

> Last checked: 2026-09-29  
> Scope: public LMCache NIXL storage implementations, the NIXL agent/plug-in model, and
> Dynamo KVBM v1 terminology. This is an ownership map, not a performance comparison.

The short version is:

> The cache manager owns KV identity, placement, capacity, and eviction. NIXL moves bytes
> between descriptors. A NIXL plug-in implements the selected transport or storage operation.

That distinction prevents several common category errors: `GDS_MT` does not create
`CacheEngineKey`, NIXL does not choose an LRU victim, and KVBM is not an LMCache storage mode.

## One data path, several owners

```text
serving engine
  creates and consumes active KV pages
        |
        v
cache manager / integration
  identity | lookup | placement | capacity | admission | eviction
        |
        v
NIXL agent
  descriptor registration | metadata exchange | transfer handle
        |
        v
NIXL plug-in
  UCX | POSIX | GDS | GDS_MT | other byte movement
        |
        v
memory, filesystem, network, or storage device
```

The concrete cache manager may be LMCache or Dynamo KVBM. The ownership boundary remains the
important part; similar-looking descriptor operations do not make the two cache managers
equivalent.

## Responsibility matrix

| Question | LMCache NIXL storage | NIXL agent / plug-in | Dynamo KVBM |
|---|---|---|---|
| KV identity | `CacheEngineKey` / object identity | Does not define KV identity | Sequence/block identity |
| Logical-to-physical placement | Backend maps key to a slot or deterministic storage identity | Uses descriptors supplied by its caller | Registry and tier-specific block pools |
| Capacity and admission | LMCache backend/cache policy | Not a NIXL cache policy | KVBM pool and block lifecycle |
| Eviction victim | LMCache policy | Not selected by NIXL | KVBM policy/lifecycle |
| Descriptor lifetime | LMCache decides when to register and release | Performs registration/deregistration | KVBM decides from tier/block lifecycle |
| Byte transfer | Requests a transfer | Builds/posts the transfer through a plug-in | Requests tier movement, commonly through NIXL |

**Source-confirmed.** NIXL exposes memory registration, descriptor lists, metadata exchange,
transfer initialization, posting, and completion. It does not prescribe a KV key, placement
policy, or eviction algorithm. See the
[NIXL backend guide](https://github.com/ai-dynamo/nixl/blob/d5c127e558fd4ddb12e47127243057339c2c2b72/docs/BackendGuide.md) (NIXL 0.10.1).

## LMCache static and dynamic are not NIXL storage modes

LMCache has used two resource-lifetime strategies for NIXL-backed storage:

```text
LMCache static storage mode
  initialize -> create/open a bounded resource pool -> register descriptors
  store      -> map key to a free slot -> transfer -> retain mapping
  evict      -> remove mapping -> return slot -> keep backing resource

LMCache dynamic storage mode
  operation  -> derive storage identity -> open/create -> register
             -> transfer -> release/deregister -> close
```

In static mode, LMCache owns the in-memory key-to-slot map and reuses registered resources. In
dynamic mode, LMCache creates descriptors on demand. The words *static plug-in* and *dynamic
plug-in* also appear in NIXL documentation, but there they describe plug-in build/loading, not
these LMCache storage lifetimes.

Use these names:

- **LMCache NIXL static storage mode** or `NixlStaticStorageBackend`;
- **LMCache NIXL dynamic storage mode** or the exact class/adapter name;
- **NIXL `GDS_MT` plug-in** for the byte-transfer implementation.

Avoid `Static NIXL`: it incorrectly assigns LMCache's policy to NIXL and is ambiguous with NIXL
plug-in loading.

**Source-confirmed.** The static file-pool lineage was introduced in
[LMCache PR #1223](https://github.com/LMCache/LMCache/pull/1223). Dynamic object work in
[PR #1939](https://github.com/LMCache/LMCache/pull/1939) closed without merge; the later
[PR #2024](https://github.com/LMCache/LMCache/pull/2024) merged. Dynamic FILE support was merged
through [PR #2936](https://github.com/LMCache/LMCache/pull/2936). Check the deployed release or
image before assuming that a merged change is present there.

## Where KVBM is similar—and where it is not

KVBM solves related lifecycle problems with explicit tier-specific block pools and block state:

```text
sequence/block identity
        -> tier block pool
        -> physical block / descriptor view
        -> NIXL transfer
```

A pre-provisioned KVBM pool can resemble LMCache's static pool, but KVBM does not name that
architecture a `static backend`. Block identity and registration state can change inside a
pre-allocated pool, and different tiers can have different lifetimes.

Limit the claim to the local subset of KVBM-like work that
`NixlStaticStorageBackend` performs: allocation, lookup, eviction, and
logical-to-physical mapping for one LMCache backend. KVBM additionally models
tier placement, block state, and movement across its serving architecture.

**Source-confirmed.** See the
[Dynamo KVBM design](https://github.com/ai-dynamo/dynamo/blob/5534a9df6d37cafe335185cc5823dfc5dfceaae2/docs/design-docs/kvbm-design.md)
and [KVBM README](https://github.com/ai-dynamo/dynamo/blob/5534a9df6d37cafe335185cc5823dfc5dfceaae2/lib/bindings/kvbm/README.md), both at Dynamo v1.0.1. The design document is not in
later releases.

## Measurement consequence

A transfer library's presence does not prove that a request used the intended tier. Verification
must bind the serving configuration, logical lookup, transfer path, and target-specific byte
counter to the same controlled request interval. See [`../METHOD.md`](../METHOD.md) and
[`../CONFIGURATION.md`](../CONFIGURATION.md).

**Inference boundary.** This ownership map does not prove which path a deployed binary selected,
whether a fallback occurred, or whether a distributed cache domain was shared. Those require
runtime configuration and path evidence.
