# source-license-headers Specification

## Purpose
Defines the meta-test-enforced SPDX license-header-presence contract for tracked first-party source files: every covered source file carries the `SPDX-License-Identifier: AGPL-3.0-or-later` token, enforced at the gate with a self-validating allowlist for genuinely header-exempt files.

## Requirements
### Requirement: SPDX License Header Presence

Every tracked first-party source file SHALL carry an SPDX license-identifier header within the first
few lines of the file, of the form (comment prefix per file type):

```
<comment> Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
```

where `<comment>` is the file type's comment token (`#` for Python / shell / Dockerfile / `#`-comment
config, `//` for Go, `{# … #}` for Jinja2-rendered templates). The normative invariant is the
presence of the `SPDX-License-Identifier: AGPL-3.0-or-later` token; the copyright line accompanies it.
For the Jinja2-rendered config/compose templates the SPDX line SHALL use a Jinja **comment**
(`{# … #}`), which the engine strips at render time — so the header licenses the template *source*
without appearing in any rendered config/compose output. The static-copied template files (the
`entrypoint.sh` script and the distro `Dockerfile.*` images, which are copied verbatim rather than
rendered) instead use their native `#` comment token, since a `{# … #}` would survive into the copied
artifact.

"Tracked first-party source file" SHALL be defined as the meta-test's covered set, built from the live
tree: Python sources under `src/` and `tests/`; Go sources under `src/templates/`; and the shipped
template sources under `src/templates/` — the immutable tooling/config plane. These templates are
rendered (or copied) under their *target* extensions (e.g. `compose.yml`, `Corefile`, `squid.conf`,
`Dockerfile.*`), NOT under a `.j2` suffix; the comment token is chosen per file type as above. There
are currently no TOML files in the covered set. Generated, vendored, or genuinely header-exempt files
(e.g. JSON seeds/fixtures with no comment syntax, a static error body served verbatim, the Go
toolchain's `go.mod`/`go.sum`, vendored manifests) SHALL be excluded via an explicit allowlist that
records a one-line reason per entry, mirroring the existing `_LAYOUT_ALLOWLIST` pattern in
`tests/unit/test_layout.py`.

A meta-test (in the spirit of `tests/unit/test_conventions.py`) SHALL enforce this at the gate: it
SHALL walk the covered set, fail if any non-allowlisted covered file lacks the SPDX token, and fail if
an allowlist entry no longer corresponds to an existing file (so the allowlist cannot rot). The
one-time header sweep across the existing tree is the migration; the meta-test is the durable
drift-prevention contract — a newly added source file without the header fails `make coverage`.

#### Scenario: New source file without header fails the gate
- **WHEN** a new `src/**/*.py` (or `src/templates/**/*.go`) file is added without the SPDX header and is not in the allowlist
- **THEN** the meta-test fails, naming the offending file, and the failure surfaces under `make coverage`

#### Scenario: Allowlisted file is exempt
- **WHEN** a covered-set file is listed in the SPDX allowlist with a one-line reason
- **THEN** the meta-test does NOT require the header on that file and the gate passes

#### Scenario: Stale allowlist entry fails the gate
- **WHEN** the SPDX allowlist contains an entry whose path no longer exists in the tree
- **THEN** the meta-test fails, so the allowlist is forced to stay current rather than silently accumulating dead exemptions

#### Scenario: Present header satisfies the requirement
- **WHEN** a covered source file begins with a comment line containing `SPDX-License-Identifier: AGPL-3.0-or-later`
- **THEN** the meta-test treats the file as compliant

#### Scenario: Jinja2 template header is a stripped comment
- **WHEN** a covered `src/templates/**` Jinja2 template carries the SPDX header as a Jinja comment `{# Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later #}`
- **THEN** the meta-test treats the template as compliant, and the rendered config/compose output produced from that template does NOT contain the header line (the Jinja engine strips `{# … #}` comments at render time)

