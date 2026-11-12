# Unified-memory lessons for KV-cache measurements

> Reviewed: 2026-11-12. Scope: measurement lessons from DGX Spark/GB10 validation,
> including earlier LMCache/vLLM and NIXL/UCX runs. This is not a benchmark or a new
> hardware-verified tier. Raw artifacts remain outside this repository under the
> [evidence policy](../EVIDENCE.md).

A portable procedure must check its assumptions about memory and data movement,
not just run the same command on another host. Four distinctions mattered here.

## 1. Logical cache tiers are not necessarily separate physical memory

**Source-confirmed.** DGX Spark uses unified memory. NVIDIA also documents access
restrictions for device allocations: shared physical memory does not make every
allocation equally accessible to CPUs or I/O devices. See the
[DGX Spark CUDA porting guide](https://docs.nvidia.com/dgx/dgx-spark-porting-guide/latest/porting/cuda.html).

A serving engine can still expose distinct CPU/GPU allocations, cache pools and
copy operations. Calling these separate cache tiers does not establish a physical
HBM-to-host-DRAM transfer or two independent memory capacities.

**Procedure consequence.** Record the allocation and copy route before interpreting
an offload. `geometry` derives logical KV bytes; it does not measure physical
movement. A CPU-cache budget is not extra physical memory to add to a GPU budget.
Copies, synchronization and allocation costs may remain even with shared memory.

**Observed.** In earlier serving runs, allocation failures accompanied missing
retrievals while responses remained correct. Reducing filler size alone did not
restore retrieval; a later configuration changing both cache capacity and filler
pressure did. This did not isolate one cause or establish a capacity threshold.
It illustrates harness/resource pressure, not a universal UMA limitation.

## 2. Discovery is not validation of a data path

**Observed.** A GPU could be discovered while its optional memory-usage query was
unsupported. A separate check found an `nvidia-fs` statistics path, but the tested
local-storage configuration did not establish a native GDS path.

**Procedure consequence.** Keep device discovery, optional telemetry and path
validation separate. The existing `doctor` and its tests preserve GPU discovery
when memory telemetry is unavailable. Its GB10 memory classification identifies
an architecture; it does not measure free cache capacity.

Check the exact filesystem, backend, device and transport before measuring a tier.
The presence of a statistics file makes a counter source a candidate, not proof of
native access. A result for local storage does not decide support for a remote
path, another software stack or a future version.

## 3. RDMA transport is not GPUDirect RDMA

**Source-confirmed.** NVIDIA's
[DGX Spark GPUDirect RDMA guidance](https://nvidia.custhelp.com/app/answers/detail/a_id/5780/)
states the platform restriction and recommends capability queries and suitable
host communication buffers. Check those capabilities rather than assuming that
a CUDA-buffer API implies direct NIC access to that allocation.

**Observed.** Retained NIXL/UCX runs established an RDMA network lane and matching
payload checks with host communication buffers. A CUDA-buffer path selected
software emulation involving `cuda_copy`. Transport success alone did not prove
GPUDirect RDMA or the absence of staging.

**Procedure consequence.** Record network transport and memory-access route as
separate facts. Follow payload direction: a NIXL `READ` can bring bytes from the
remote node into the initiator, so inspect both endpoints' counters. NIC bytes
can include setup and protocol traffic; they are not automatically logical KV bytes.

These observations do not add a transport provider to `kvprobe ingest`. Network
lanes and port counters alone do not establish the current Gate 1 contract, a
storage-tier result, remote media reads or shared-cache correctness.

## 4. One completed transfer is not repeated application validation

**Observed.** A standalone CUDA-buffer write completed with payload verification.
In a subsequent application configuration, an initial request succeeded but a
later request failed in the CUDA-copy path. A host-buffer application configuration
completed its limited request sequence. The failing run's root cause was not isolated.

**Procedure consequence.** Test the operation direction, receiving payload and
repeated buffer use in the actual application path. Preserve failed requests and
source versions. A standalone copy checks a smaller boundary than serving, and a
short successful sequence does not establish a concurrency envelope.

This is not evidence that CUDA buffers always fail or that host buffers always
perform better. It is a reason to validate the chosen route under repeated use.

## Carry the distinctions into the five gates

| Gate | Check on a unified-memory or network-backed path |
|---|---|
| **1 — Service** | Identify the source allocation/tier and controlled request interval. A framework hit or transport completion alone is insufficient. |
| **2 — Retrieval** | Match the runtime and allocation route against recompute. Include the actual copy and synchronization costs. |
| **3 — Admission** | Retain every store/reuse attempt, including allocation failures and fallback. |
| **4 — Capacity** | Distinguish logical pool budgets from shared physical capacity; separate resource pressure from verified lower-tier service. |
| **5 — Envelope** | Keep correctness and tier evidence at every declared load level. A transfer smoke test is not an operating-envelope measurement. |

The gates do not change because a host has unified memory. The evidence needed to
answer them must fit the actual path. CPU-memory service and distributed paths
remain outside the hardware-verified scope described in [README](../../README.md#scope--what-is-verified-today).
Use [portability validation](../PORTABILITY.md) for the workflow and
[measurement profiles](../PROFILES.md) for the boundary between a component tier
and a distributed path.
