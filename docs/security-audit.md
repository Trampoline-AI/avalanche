# Dependency security audit — 2026-10-06

## Result and scope

The starting branch had **16 distinct known dependency vulnerabilities across
9 packages**: 15 open GitHub Dependabot alerts and one additional Click advisory
reported by PyPI/pip-audit. The updated lockfiles remove **15 findings**.
**DiskCache remains vulnerable and has no published patched release.** It is not
silently ignored or dismissed.

Sources: [repository Dependabot alerts](https://github.com/Trampoline-AI/avalanche/security/dependabot),
PyPI's per-version vulnerability records for all 208 registry package/version
pairs in `uv.lock` (including platform/Python alternatives), `pip-audit` against
an all-extras/all-groups export, and `pnpm audit` against `web/pnpm-lock.yaml`.
CVE, GHSA and PYSEC aliases are counted once. pip-audit currently emits duplicate
DiskCache and virtualenv records: its raw baseline count of 17 is not 17 distinct
Python vulnerabilities. The final raw count of 2 represents the same DiskCache
advisory twice. The final web audit reports zero vulnerabilities.

This is a known-dependency audit plus a targeted review of storage, model-cache,
and operator trust boundaries, not an exhaustive penetration test or a guarantee
that the repository has no other vulnerabilities. Fixes apply to the committed
lockfiles. Wheel consumers resolve dependencies independently; audit their actual
installed environment rather than assuming this development lock is enforced.

## Work categories and actions

### A. Compatible package updates — low integration effort

All changes below stay within the existing parent dependency requirements. Only
lockfile versions changed; no new overrides or global dependency refresh were
needed. Patch releases are lower risk, not proof of identical behavior.

| Package and locked change | Advisory / failure | Required action and status |
| --- | --- | --- |
| `source-map-js` 1.2.1 → 1.2.2 | [GHSA-68fv-2mgg-jv7q](https://github.com/advisories/GHSA-68fv-2mgg-jv7q): indexed source-map offsets can block the event loop; high | **Fixed.** Refresh transitive patch; rebuild browser assets. |
| `brace-expansion` 5.0.9 → 5.0.12 | [GHSA-q2hr-2g5m-vwhr](https://github.com/advisories/GHSA-q2hr-2g5m-vwhr): quadratic brace rewriting; medium | **Fixed.** 5.0.12 covers this and both following advisories. |
| `brace-expansion` 5.0.9 → 5.0.12 | [GHSA-qhr7-859c-m2p7](https://github.com/advisories/GHSA-qhr7-859c-m2p7): nested-brace stack exhaustion; high | **Fixed.** Minimum patch for this advisory alone is 5.0.11. |
| `brace-expansion` 5.0.9 → 5.0.12 | [GHSA-6j4f-fj2g-mc7p](https://github.com/advisories/GHSA-6j4f-fj2g-mc7p): comma-parser stack exhaustion; high | **Fixed.** Minimum patch for this advisory alone is 5.0.10. |
| `werkzeug` 3.1.8 → 3.1.9 | [GHSA-g6x2-hccm-hh4m](https://github.com/advisories/GHSA-g6x2-hccm-hh4m): Windows NTFS device-name file-serving hang; medium | **Fixed.** Compatible patch for Flask/Moto development dependencies. |
| `virtualenv` 21.7.4 → 21.7.13 | [GHSA-p58f-9548-mpm2](https://github.com/advisories/GHSA-p58f-9548-mpm2): shell command injection through activation-script paths; high | **Fixed.** 21.7.13 covers all four virtualenv advisories. |
| `virtualenv` 21.7.4 → 21.7.13 | [GHSA-x78j-v8h9-3j2q](https://github.com/advisories/GHSA-x78j-v8h9-3j2q): Windows batch prompt injection; high | **Fixed.** Minimum patch for this advisory alone is 21.7.12. |
| `virtualenv` 21.7.4 → 21.7.13 | [GHSA-94p9-xgh2-xp45](https://github.com/advisories/GHSA-94p9-xgh2-xp45): downloaded seed wheels lack integrity verification; high | **Fixed.** Minimum patch is 21.7.12. Upstream deliberately skips the new public-PyPI digest check for configured custom indexes; those indexes still need to be trusted. |
| `virtualenv` 21.7.4 → 21.7.13 | [GHSA-9h9j-4vrj-gf7g](https://github.com/advisories/GHSA-9h9j-4vrj-gf7g): prompt line boundaries inject `pyvenv.cfg` entries; medium | **Fixed.** Minimum patch for this advisory alone is 21.7.11. |
| `litellm` 1.91.1 → 1.91.5 | [GHSA-3cv6-jpf6-8222](https://github.com/advisories/GHSA-3cv6-jpf6-8222): proxy request routing can leak provider credentials and perform server-side request forgery; medium | **Fixed.** Use the patched 1.91 maintenance line, not an unnecessary newer feature release. |
| `urllib3` 2.7.0 → 2.8.0 | [GHSA-gh4c-6fx4-qh6g](https://github.com/advisories/GHSA-gh4c-6fx4-qh6g): chunked Deflate stream infinite loop; medium | **Fixed.** Compatible minor update; retain the existing HTTP clients. |
| `urllib3` 2.7.0 → 2.8.0 | [GHSA-vxq7-64xx-v4gw](https://github.com/advisories/GHSA-vxq7-64xx-v4gw): unbounded chunk-size line buffering; high | **Fixed.** Same 2.8.0 update. |
| `urllib3` 2.7.0 → 2.8.0 | [GHSA-8988-9cw3-xx77](https://github.com/advisories/GHSA-8988-9cw3-xx77): HTTPS proxy TLS settings may be ignored/overridden; high | **Fixed.** Same 2.8.0 update. |
| `click` 8.1.8 → 8.3.3 | [PYSEC-2026-2132 / CVE-2026-7246](https://github.com/pypa/advisory-database/blob/main/vulns/click/PYSEC-2026-2132.yaml): `click.edit()` command injection | **Fixed.** Compatible minor update. Exercise CLI commands because Click also changed behavior across 8.2/8.3. Not present in the initial open Dependabot alerts. |

Exposure and dependency chains:

- `source-map-js` is reached through Vite/PostCSS, Tailwind and jsdom tooling;
  `brace-expansion` through ESLint/typescript-eslint → minimatch. Both are
  development/build dependencies here, not browser runtime dependencies. Only
  brace-expansion's 5.x line was locked. Untrusted tooling inputs still matter.
- Werkzeug is pulled by development Flask/Moto; Avalanche's HTTP listener uses
  FastAPI/Uvicorn, not Werkzeug. The specific hang requires Windows/NTFS.
- virtualenv is used by development pre-commit and optional Ray's default extra.
  Creating/activating environments and trusting package indexes are the relevant
  boundaries, not ordinary workflow payload parsing.
- LiteLLM is reached through PredictRLM/DSPy and the `codex-lm` extra. No LiteLLM
  proxy server is started by repository code; this advisory concerns that proxy,
  not proof of a reachable credential leak in Avalanche's model SDK calls.
- urllib3 is shared by requests and AWS/storage dependencies and is a real runtime
  HTTP dependency. Click is used transitively by PyIceberg and other tooling;
  no repository Python call to `click.edit()` was found.

### B. Coordinated storage upgrade — breaking-change review required

**[GHSA-27vj-qcqg-25rc](https://github.com/advisories/GHSA-27vj-qcqg-25rc)**,
`fsspec` template injection leading to Python code execution, high severity.

**Fixed:** `fsspec` 2023.12.2 → 2026.6.0, the first patched release. This is not a
standalone package bump: `s3fs` constrains fsspec to its matching release line,
and this repository previously required development `s3fs<2024` and
`boto3<1.36`. Update those requirements to `s3fs>=2026.6.0,<2027` and
`boto3>=1.35,<2` and resolve the AWS clients together:

| Package | Before | After |
| --- | --- | --- |
| s3fs | 2023.12.2 | 2026.6.0 |
| fsspec | 2023.12.2 | 2026.6.0 |
| aiobotocore | 2.17.0 | 3.9.2 |
| boto3 / botocore | 1.35.93 | 1.43.106 |
| s3transfer | 0.10.4 | 0.19.2 |

This crosses an aiobotocore major version and several calendar-versioned storage
releases. The patched reference filesystem skips `gen` expansion in default
`simple_templates=True` mode. Explicit `simple_templates=False` uses sandboxed
Jinja templates: ordinary variable expansion succeeds; access to Python globals
is rejected. No repository caller uses `ReferenceFileSystem`, so no application
migration was needed. Workflows supplied by users that use that API must review
this behavior change and must not restore unsandboxed rendering.

PyIceberg itself stays at 0.10.0. A live local Moto HTTP server verified boto3 and
s3fs interoperability plus PyIceberg `FsspecFileIO` create/read/delete operations.
This is S3-protocol fixture coverage, not a live AWS S3/Glue validation.

The virtualenv patch also requires `python-discovery` 1.5.1 → 1.6.1. No other
Python packages were refreshed.

### C. No upstream patch — operational mitigation or dependency replacement

**[GHSA-w8v5-vhqr-4h9v / CVE-2025-69872](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v)**,
`diskcache` 5.6.3, medium severity. PyPI still publishes 5.6.3 as the latest
release and lists no fixed version.

The chain is `avalanche-ai` → `predict-rlm` 0.9.0 → `dspy` 3.2.1 → `diskcache`.
DSPy enables disk caching by default in `dspy/clients/__init__.py`, under
`DSPY_CACHEDIR` or `~/.dspy_cache`. Its `Cache` creates a `diskcache.FanoutCache`
and reads entries in `dspy/clients/cache.py`. The default serializer uses pickle,
a Python format whose deserialization can execute code. An attacker needs write
access to the cache directory/database; sending an ordinary model prompt alone
is not the prerequisite described by this advisory.

This is **not unfixable in principle**, but no version bump clears it today:

1. **Immediate operational mitigation:** use a fresh, private, user-owned cache
   directory with mode 0700 and trusted parent directories. Set `DSPY_CACHEDIR`
   before importing DSPy/PredictRLM. Do not restore model caches from untrusted CI
   artifacts or share them with untrusted users. Changing permissions after
   tampering does not make existing cached values safe; use a fresh directory.
2. **Disable persistent model caching when it is unnecessary:** configure DSPy
   before model calls in every execution process:

   ```python
   import dspy

   dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=True)
   ```

   This is a process-local setting, not a universal fix for DiskCache. DSPy's
   import already constructs its default cache, so protect the directory even
   when subsequently disabling caching. A setting applied only in a parent
   process must not be assumed to configure a spawned operator/Ray worker.
3. **Optional upstream mitigation:** DSPy 3.2.1 also exposes
   `dspy.configure_cache(restrict_pickle=True)`. Its allowlisted deserializer is
   defense in depth, not a reason to trust an attacker-writable database or to
   claim the underlying DiskCache advisory is fixed. Custom cached types may
   require migration. The PR does not silently change application-wide DSPy
   caching behavior.
4. **Dependency removal/replacement:** fully removing the alert requires a
   patched DiskCache release or a DSPy/PredictRLM change that removes/replaces
   that dependency. Forcing an imaginary patched version or deleting the lock
   entry leaves an invalid installation. Replacing the agent dependency is a
   separate API/behavior migration, not a simple security bump.

Keep this finding visible and recheck upstream releases. The operator's own
`discovery_cache.py` stores JSON/Pydantic data and is not this DiskCache path.

## Operator trust boundary

The targeted source review found loopback-first, locally unauthenticated
operator services. gRPC can bind outside loopback with a warning; the browser
listener requires acknowledgment of an external trusted proxy for non-loopback
binding. Keep operator, REST, gRPC-Web and webhook listeners on loopback unless
an authenticated external boundary is actually configured. Dependency updates do
not add production authentication, multi-tenancy, or safety for untrusted
workflow Python code. These are existing operational constraints, not additional
confirmed dependency CVEs.

## Verification

Completed on Linux/Python 3.13:

- `uv sync --locked --all-extras` and `uv pip check` — installed graph compatible.
- `uv run pytest test/iceberg/namespace_test.py test/storage/test_table_contracts.py test/agent test/operator_tests/test_web.py -q` — 126 passed.
- `make smoke-test` — 7 passed, including CLI/gRPC and documented example paths.
- `make web-lint` and `make web-build` — passed; existing large-chunk warning only.
- `uv build` — source distribution and wheel built successfully.
- `npx --yes pnpm@10.17.1 --dir web audit --json` — zero advisories.
- Before/after reference-template smoke: old fsspec exposed Python `os` globals;
  patched defaults skip generator expansion, explicit complex mode rejects the
  same access, and ordinary variable expansion works in complex mode.
- S3 smoke against a local Moto HTTP server: boto3 bucket creation, s3fs
  write/read, boto3 cross-read, and PyIceberg `FsspecFileIO` create/read/delete.
- Documented DSPy memory-only configuration: cached a request/response and read
  it back with persistent caching disabled, using a private temporary directory
  established before import. No model-provider request was made.
- Real Chromium against `ava operator examples/operator_workflow.py`: discovered
  workflow, clicked Run, observed all four nodes and terminal run state succeed,
  and captured the rendered DAG/logs. One snapshot request was aborted during
  the transition; the run completed successfully.
- Final all-version PyPI audit — only DiskCache, including Python/platform
  alternatives not installed on this machine.

Reproduce the Python audit without suppressing the remaining finding:

```sh
uv export --locked --all-extras --all-groups --no-emit-project --no-hashes \
  --format requirements-txt --output-file /tmp/avalanche-audit-requirements.txt
uvx pip-audit -r /tmp/avalanche-audit-requirements.txt --no-deps --disable-pip \
  --progress-spinner off --format json
```

The export is fully pinned; `--no-deps` audits those entries without re-resolving
another environment. pip-audit's nonzero exit is expected while DiskCache remains.
`uv-secure` was also attempted but returned registry lookup errors, not an audit
result; it was replaced by pip-audit and direct per-version PyPI queries.

Not exercised: live model-provider requests, live AWS/Glue, Windows activation or
NTFS behavior, real Ray workers, tmux/TUI, and Python 3.11/3.12 execution. Existing
DSPy deprecation and optional PyIceberg decoder warnings appeared in focused
checks. No claims of validation on those unexercised surfaces are implied.
