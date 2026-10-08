# Provider caching: why these boundaries matter

Read this before changing `env`, absolute-path resolution, request projections,
or execution plans. These paths share validation, but serve different purposes.

## An installed binary should not need rediscovery

Images install dependencies by symbolic name, then plugin configuration replaces
that name with the installed absolute path. The first runner request therefore
need not have the same cache key as the install request. In October 2026 this
caused about 11.5 seconds of repeated version/hash probes on Cabbage even though
the named cache entries were present and valid. The missing step was reusing the
validated named request for its exact resolved path, not installing more packages.

`config._load_cached_request_projection` first checks the exact request, then
allows a matching absolute-path record to supply its original name. Every other
request option still participates in the key. `_cached_records` checks the
provider, requested path, cache-file trust and fingerprints;
`_validated_cached_plan` checks the execution context. Do not replace this with a
basename-only lookup or globally collapse names and paths in the request key:
two installations can share a name while having different runtimes or options.

Hydration can also turn `FOO_BINARY=foo` into that exact cached path. Only this
proven bound selector may be treated as equivalent. PATH, VIRTUAL_ENV, provider
options, versions, roots, effective UID and other resolution inputs still matter.
A different temporary `uv --with-editable` environment is a legitimate cache
miss; compare warm runs in the same environment.

## Ownership is distinct from projection and execution

* Each provider owns its installation, bootstrap dependencies and runtime
  environment. Do not prepend `env` to every resolution chain or combine every
  provider's PATH/PYTHONPATH. An ordinary host tool must not inherit an unrelated
  managed Python environment.
* `env` can own an `env/bin` projection that points at another provider's binary.
  That projection retains the resolved provider and its runtime. This does not
  make `env` the owner of the underlying installation. Declining to cache a
  foreign file must not invalidate an existing projection with the same name.
* An explicit path is pinned, including on a cold cache. `Binary.name` normalizes
  to a basename, so `BinaryService._overrides_for_event` preserves the path in
  provider overrides. A missing explicit file must not select a different PATH
  candidate or install a replacement. Skipping `env` for all managed absolute
  paths would also bypass supported explicit-path resolution.
* Metadata returns a stable public projection path. Execution may peel abxpkg
  `env/bin` aliases with `resolve_env_projection`, but must stop at the actual
  launcher or Python venv. Blanket `realpath()` at the execution boundary can
  discard `pyvenv.cfg` discovery or break launchers that use argv[0]. Canonical
  paths used for identity comparison are not automatically valid launch paths.
* Script requests retain their schema's declared name: an alias need not equal
  the executable basename. The script helper validates that a declared-name
  cache result resolves to the requested path before using it.

## A warm plan is validated evidence, not a frozen environment

Cache reads require trusted regular files with appropriate ownership and modes,
matching filesystem fingerprints, request identity, runtime identity and PATH
resolution. Provider checks must still validate actual installed package metadata
and launchers, not merely a desired version from an override. Preserve these
checks when optimizing the hot path; an invalid or ambiguous record must miss.

Execution plans retain provider environment changes and their relevant inputs;
validation starts with the current caller environment. Freezing the entire old
environment would leak unrelated caller state into subsequent invocations.
`no_cache` remains an explicit request for fresh resolution. Provider setup and
external work happen outside cache mutation locks: bootstrap can enter another
provider, and holding a lock across it previously risked deadlocks.

## Installation and cache locks have separate lifetimes

Complete install, update and uninstall operations serialize on the root's
`.install.lock`, including bootstrap and external commands. Cache mutations
serialize on the existing `.lock` and retain their validation and ownership
checks. Holding a cache lock for the complete installation caused Env to wait
for Apt while Apt waited for Env during parallel real dependency requests.
The separate lifecycle lock preserves installer serialization without blocking
cross-provider metadata reads behind an external install.

When upgrading from versions that used `.lock` for the complete lifecycle,
stop and restart all processes sharing the provider library. Old and new
processes use different installation lock namespaces and cannot guarantee
mutual exclusion between their complete installations.

## History and the failures the current design avoids

These commits explain why apparently simpler approaches were changed. The rules
above describe the current contract; the links are supporting history.

| Change | Lesson retained |
| --- | --- |
| [d6599d3](https://github.com/ArchiveBox/abxpkg/commit/d6599d3), [8a47409](https://github.com/ArchiveBox/abxpkg/commit/8a47409) | Resolution and bootstrap chains stay provider-owned. |
| [87a7ca2](https://github.com/ArchiveBox/abxpkg/commit/87a7ca2) | Unrelated provider environments can corrupt host Python tools. |
| [7fe905e](https://github.com/ArchiveBox/abxpkg/commit/7fe905e), [7723a6f](https://github.com/ArchiveBox/abxpkg/commit/7723a6f) | Warm reads must validate trust, effective options and ambient PATH changes. |
| [1ea22be](https://github.com/ArchiveBox/abxpkg/commit/1ea22be), [c1209a9](https://github.com/ArchiveBox/abxpkg/commit/c1209a9), [5f83a3d](https://github.com/ArchiveBox/abxpkg/commit/5f83a3d) | Python caches cannot cross incompatible virtual environments. |
| [a1bbc71](https://github.com/ArchiveBox/abxpkg/commit/a1bbc71), [PR #44](https://github.com/ArchiveBox/abxpkg/pull/44) | Script hydration and repeated absolute requests needed cache reuse; these did not cover every first name-to-path service request. |
| [c103c08](https://github.com/ArchiveBox/abxpkg/commit/c103c08) | Explicit paths must survive basename normalization. |
| [3543e1f](https://github.com/ArchiveBox/abxpkg/commit/3543e1f) | Refusing ownership of a foreign file must not delete a valid runtime projection. |
| [722b4e4](https://github.com/ArchiveBox/abxpkg/commit/722b4e4), [7883594](https://github.com/ArchiveBox/abxpkg/commit/7883594) | Peel foreign aliases only for execution; preserve venv launch paths and stable metadata. |
| [aeb010e](https://github.com/ArchiveBox/abxpkg/commit/aeb010e) | Keep setup outside mutation locks to avoid bootstrap lock cycles. |
| [82b9b04](https://github.com/ArchiveBox/abxpkg/commit/82b9b04) | Reuse named install evidence for the first exact absolute-path service request without weakening ownership or validation. |

## Verification that exercises these contracts

`tests/test_binary_service.py::test_binary_service_reuses_absolute_path_request_projection`
covers both repeated absolute requests and named installation followed by an
absolute request, with and without bound selector hydration. Real resolution is
profiled to prove the second request avoids the cold loader.

The CLI tests for explicit managed binary requests, runtime projections, Python
environments and cache-context changes protect the surrounding contracts. For an
integration check, run the normal `abx-dl install` flow by name, then again with
the plugin's binary setting pointing at the installed path, in the same environment.
Measure cold-loader calls as well as elapsed time. A locally warm run is not proof
of an image fix: verify the deployed package versions, runtime UID/environment,
and a real user submission on the resulting image.
