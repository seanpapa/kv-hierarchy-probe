# SPDX-License-Identifier: Apache-2.0
"""Reference backend identities used by the bundled LMCache experiments.

The evidence schema intentionally accepts any non-empty backend string so an
external serving stack can describe its own path.  This registry is narrower:
it is the machine-readable source for the six LMCache paths documented and
produced by this repository's reference experiments.
"""

REFERENCE_LMCACHE_BACKEND_IDS: dict[str, str] = {
    "native_posix": "lmcache_native_posix",
    "nixl_static_posix": "lmcache_nixl_static_posix",
    "nixl_dynamic_posix": "lmcache_nixl_dynamic_posix",
    "native_gds": "lmcache_native_gds",
    "nixl_static_gds_mt": "lmcache_nixl_static_gds_mt",
    "nixl_dynamic_gds_mt": "lmcache_nixl_dynamic_gds_mt",
}
