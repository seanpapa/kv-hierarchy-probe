# Portability validation

Portability means that the probe identifies unsupported assumptions before it emits a performance
claim. It does **not** mean that every machine must expose the same counters or pass the same tier.

Portability across hosts and backends is separate from execution topology and cache scope. The
current evidence can port a component-tier measurement within one endpoint and cache domain, but
those properties are not encoded as profile, topology, or cache-scope fields today. Running it on
two nodes does not make the result a disaggregated-path measurement: that requires correlated
producer/consumer evidence, transfer attribution, placement identity, and complete-path timing as
defined in [measurement profiles](PROFILES.md).

## Validation sequence

Start with the same guided entry point on every new host and preserve its outputs with the run:

```bash
python3 -m kvprobe first-visit --output-dir portability-first-visit
```

Keep `portability-first-visit/doctor.json` and the generated reports with the evidence bundle.
The guided command runs `doctor`, which distinguishes a missing executable, an unsupported query,
and a capability that has not yet been tested; it also confirms that the bundled reporter fails
closed on its reference cases. It does not certify GDS, RDMA, or serving tier verification: those
require a target-specific transfer and physical evidence. Use `kvprobe doctor --json-output
capability.json` separately only when a workflow needs the manifest without the guided reports.

Then answer, in order:

1. Is GPU memory physically separate from host memory, unified, or still unknown?
2. Which external signal can attribute every request on this platform?
3. **What names the device a run measured, stably, on this platform?**
4. Which of the current scripts can run unchanged?
5. Which gate needs a platform adapter rather than a skipped check?
6. Does the generated report suppress claims when that adapter is absent?

## Naming the device on a new platform

Question 3 is a precondition for emitting evidence at all, not a nicety. Storage byte counters do not identify the
accelerator that issued the I/O, even when filtered to one block device, so
`runtime.device_id` is required and the fingerprint covers it. Two devices that reach the tier at
different cost would otherwise share one fingerprint and merge into a single document.

`device_id` is deliberately **an opaque stable string, not a vendor address format**. Requiring one
platform's tool output would put every other platform outside the contract, which is the opposite
of portability. So a new platform does not need `nvidia-smi`, or PCI addressing at all — it needs
one identifier that differs between devices and does not drift during the campaign:

- a PCI address where the platform prints one, in whatever form its own tool prints it;
- an integrated or unified part's device name where there is no meaningful bus address;
- any identifier the operator can keep stable, when the platform exposes nothing better.

Two failure modes to check before trusting a run on new hardware:

- **An identity that drifts.** Enumeration indices are not stable across reboots or driver
  changes, which is why `gpu_index` is optional and never the identity. If the only available value
  drifts, the fingerprint separates runs that were in fact comparable.
- **An identity that does not distinguish.** A value shared by every device on the host — a product
  name, a driver version — satisfies the field while defeating its purpose. Confirm it differs
  between the devices actually present.

Record the value verbatim. The fingerprint is taken over its text, so case and spacing are part of
the configuration; surrounding or control whitespace is refused rather than trimmed, because silent
trimming would hide exactly that drift. See
[Configuration identity](CONFIGURATION.md#why-the-device-is-part-of-the-identity).

## DGX Spark — lessons and remaining validation

Earlier validation on DGX Spark exposed assumptions from discrete-memory GPU
servers. The [unified-memory lessons](reference/UNIFIED-MEMORY-LESSONS.md) separate
those observations from current source-confirmed facts and unverified conclusions.
They are not certification of the entire public baseline on this platform.

Already represented in the public baseline:

- GPU discovery is independent of the optional `nvidia-smi memory.used` query;
  an unsupported query does not turn a detected GPU into a CPU-only host.
- The GB10 classification is `unified`, with an explicit confidence/basis. This
  does not measure free memory or prove how a serving engine allocates its caches.
- `runtime.device_id` accepts an opaque non-PCI identity; the bundled
  [`unfiltered-counter example`](../examples/evidence/attribution-unfiltered-counter.json)
  illustrates that shape. A valid identity field does not repair insufficient attribution.

For each new runtime or data path, still verify:

- Which logical allocations share physical memory, and which copies still occur.
- The exact filesystem/backend/device/transport path, rather than module presence.
- External evidence at the intended scope; unavailable evidence stays unsupported.
- Correctness and repeated buffer use in the application, beyond a transfer smoke test.
- Device identity stability and the complete first-visit workflow on the target host.

Preserve the manifest, console output, configuration, evidence and checksums.
Turn platform-specific findings into capability rules or adapter tests, rather
than private host-name special cases. Earlier experiments do not establish CPU-memory
tier service, a remote-tier performance claim or a distributed operating envelope.

## Release boundary

Public repository access and a versioned release are separate steps. This note
adds lessons, not a new release or hardware-verification claim. Follow
[RELEASING.md](RELEASING.md) and record which tree and paths were actually checked
before tagging; an earlier platform run is not validation of later changes.
