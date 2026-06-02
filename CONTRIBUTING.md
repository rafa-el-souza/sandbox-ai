# Contributing to sandbox-ai

Thanks for your interest in `sandbox-ai`. This is a security tool with a
deliberately small, deterministic core, so contributions are held to a high bar
for clarity, isolation, and test coverage. This guide covers the development
workflow, the gate every change must pass, and the one-time CLA step.

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

## Contributor License Agreement (CLA)

`sandbox-ai` requires every contributor to agree to a Contributor License
Agreement before their contribution can be merged. We use the **Apache Software
Foundation ICLA and CCLA**, served verbatim:

- **Individuals** — the [Individual CLA](docs/legal/apache-icla.md).
- **Contributing on behalf of an employer / corporation** — the
  [Corporate CLA](docs/legal/apache-ccla.md).

We collect signatures with **[CLA Assistant](https://cla-assistant.io/)**, so
signing is a one-click step in your pull request:

1. Open your pull request as usual.
2. The CLA Assistant bot comments on the PR with a link.
3. Click it, review the agreement, and sign in with your GitHub account. Your
   signature is recorded once and applies to all future PRs.
4. The CLA check on the PR turns green, and your PR becomes mergeable.

If you are contributing as an employee and your employer holds rights to your
work, your organization should also have a signed Corporate CLA on file — see
the [CCLA](docs/legal/apache-ccla.md).

> **Maintainer note (out-of-band setup).** The CLA Assistant GitHub App is
> configured by the maintainer outside the repository: install the app on the
> repo and point it at the CLA text published from
> [`docs/legal/`](docs/legal/). No in-repo workflow file is required for the
> hosted CLA Assistant app; the verbatim agreements in `docs/legal/` are the
> source of truth for the signed text.

## License

By contributing, you agree that your contributions are licensed under the
project's [AGPL-3.0-or-later license](LICENSE), consistent with the CLA above.
