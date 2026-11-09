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

Question 3 is a precondition for emitting evidence at all, not a nicety. Because the byte counters
this tool reads are host-wide, a normalized document cannot say which accelerator produced it, so
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

## First portability target — DGX Spark

DGX Spark is deliberately the first validation target because GB10's coherent unified system
memory breaks assumptions learned on a discrete GPU server. The initial validation is successful
when the tool reports those differences honestly, even if no storage-tier performance result is
publishable.

Expected checks, not pre-recorded results:

- GPU discovery succeeds independently of the optional `nvidia-smi memory.used` query.
- GB10 is classified as `unified`; an unsupported memory-usage query is not treated as “no GPU.”
- Absence of `nvidia-fs`, GDS, or GPUDirect attribution leaves the current adapter `limited` rather
  than silently substituting framework counters.
- LMCache or KVBM host offload, if exercised, is described as a logical allocation path; it is not
  reported as a discrete HBM→host-DRAM tier without physical separation evidence.
- A `runtime.device_id` can be recorded for GB10 without assuming PCI addressing or `nvidia-smi`
  output, and it is stable across a reboot. The bundled
  [`attribution-unfiltered-counter.json`](../examples/evidence/attribution-unfiltered-counter.json)
  example already carries a non-PCI identity for a unified-memory Arm64 host.
- The GPU-free `first-visit` journey and automatic reports run unchanged on Arm64.

After the first run, preserve the manifest, console output, normalized evidence, raw evidence, and
checksums. Any Spark-specific fix should become a general capability rule or adapter test; avoid a
host-name special case.

## Release boundary

The CPU-only CI path runs before Spark validation. Public `v0.1.0` release preparation happens
after the Spark findings are folded back into capability discovery, documentation, and tests.
