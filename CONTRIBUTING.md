# Contributing to sandbox-ai

Thanks for your interest in `sandbox-ai`. This is a security tool with a
deliberately small, deterministic core, so contributions are held to a high bar
for clarity, isolation, and test coverage. This guide covers the development
workflow, the gate every change must pass, and how contribution licensing works.

## Before you start

- Read the [README](README.md) for what the project is and its
  [threat model](README.md#threat-model), and [SECURITY.md](SECURITY.md) for the
  reporting process and known limitations.
- For anything non-trivial, **open an issue first** to discuss the approach. The
  project follows an OpenSpec-driven change workflow (see
  [`CLAUDE.md`](CLAUDE.md) and `openspec/`), and a quick design conversation up
  front saves a rewrite later.
- **Do not** report security vulnerabilities in a public issue or PR — follow
  [SECURITY.md](SECURITY.md).

## Development setup

All commands require [`uv`](https://docs.astral.sh/uv/). The project pins Python
3.14 via `.python-version`; the host's system Python may differ. **Always** use
`uv run` or the `make` targets — never invoke `python` directly.

```bash
make test        # run the unit suite
make coverage    # coverage report (target: 100% on core/ and cli/)
make lint        # ruff
make typecheck   # mypy (strict)
```

See [docs/testing.md](docs/testing.md) for the full testing, coverage, lint, and
typecheck conventions, including the enforced test conventions.

## Architecture orientation

The codebase is documented by concern under [`docs/`](docs/):

- [docs/architecture.md](docs/architecture.md) — the `src/core/` module map.
- [docs/privilege-boundary.md](docs/privilege-boundary.md) — the load-bearing
  privilege boundary (read this before touching anything that crosses it).
- [docs/dispatcher.md](docs/dispatcher.md) — the dispatcher ops and how to add a
  new orchestrator-to-sandbox operation.
- [docs/configuration.md](docs/configuration.md),
  [docs/acl-model.md](docs/acl-model.md), [docs/locking.md](docs/locking.md),
  [docs/setup.md](docs/setup.md) — the remaining subsystems.

## The bar for a change

Before opening a PR, make sure your change:

- **Passes the full gate** — `make lint typecheck test coverage` green. Coverage
  is enforced at 100% on `core/` and `cli/`; add tests that assert *behavior*,
  not tests that merely satisfy the metric.
- **Preserves the privilege boundary.** Any code that shells out must go through
  the sanctioned execution path; any orchestrator→sandbox operation must cross
  the boundary via the dispatcher. Never add a path that runs Docker as the
  operator.
- **Is focused and immutable in spirit** — small, single-purpose files; prefer
  returning new values over mutating shared state; handle errors explicitly with
  actionable messages.
- **Updates docs/specs** when it changes behavior. For spec-level changes, follow
  the OpenSpec workflow in `openspec/`.

Use clear, conventional commit messages (e.g. `feat:`, `fix:`, `docs:`,
`refactor:`). Keep commits coherent and reviewable.

## Licensing of your contributions

`sandbox-ai` follows the standard open-source **inbound = outbound** model:
when you contribute, your contribution is licensed under the project's existing
license, **AGPL-3.0-or-later** (see [`LICENSE`](LICENSE)). **There is no
Contributor License Agreement to sign** — opening a pull request is itself your
agreement to license your contribution under AGPL-3.0-or-later. This is the same
rule encoded in [GitHub's Terms of Service](https://docs.github.com/site-policy/github-terms/github-terms-of-service#6-contributions-under-repository-license)
for contributions to a repository that carries a license.

Please only submit work you have the right to contribute under this license. If
your contribution incorporates third-party code, call it out in your pull
request along with its source and license so we can review compatibility.
