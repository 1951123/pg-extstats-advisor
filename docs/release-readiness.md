# Release-readiness checklist

This checklist records the current research-prototype release candidate. It
does not confer production-ready or stable-1.0 status.

## Build

- [x] Python wheel build is defined by `pyproject.toml`.
- [x] Docker clean-room build provenance is tracked in
  `experiments/m2-33-docker-cleanroom/build-provenance.json`.
- [x] PostgreSQL 16.14 upstream tarball and patch hashes are recorded.
- [x] The advisor image uses a source-built patched backend; production and
  validation images use stock PostgreSQL.

## Test

- [x] Canonical command: `python -m pytest -q` from the repository root.
- [x] `ruff check .` and `git diff --check` are clean.
- [x] Installed-wheel CLI help smoke is covered by the release audit.
- [x] M2.33 clean-room lifecycle summary is `PASS` for all positive and
  negative gates.

The repository contains an empty `tests/__init__.py` so the canonical local
test package wins over unrelated installed packages also named `tests`. This
removes the previously observed bare-pytest namespace collision without a
package restructure.

## Product

- [x] Supported and unsupported scope is canonicalized in
  `docs/supported-scope.md`.
- [x] Docker quick start is documented in `docs/docker.md`.
- [x] The DBA-controlled workflow is documented in `docs/dba-runbook.md`.
- [x] Read-only preflight, deployment verification, and rollback verification
  are documented and evidenced.
- [x] Artifact contracts and fixed-target semantics are documented.

## Security and privacy

- [x] Runtime credentials are generated/injected and excluded from evidence.
- [x] Capture and recommendation sensitivity warnings are explicit.
- [x] No secret manager, encryption, centralized authentication, network
  policy, or automatic retention is claimed.
- [x] Large historical artifacts were audited; no Docker image tar, PG data
  directory, raw runtime log, or credential was added by M2.34.

## Research integrity

- [x] Claims and non-claims are mapped in `docs/claims-to-evidence.md`.
- [x] Historical experiment artifacts remain unchanged.
- [x] Target-grid, reservoir/native, acquisition-fidelity, and robustness
  studies are labeled historical or bounded evidence.
- [x] Global optimality, universal target optimality, future-sample equality,
  latency improvement, and arbitrary-version support are explicitly excluded.

## Release boundary

- [x] RC manifest exists at `release/rc-manifest.json`.
- [x] No tag, GitHub Release, registry push, or PyPI upload is performed.
- [ ] A future author/PI decision is still required before creating a tagged
  prototype release.
