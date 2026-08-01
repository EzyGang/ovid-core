# Repository Guidelines

## Project Overview

`ovid-core` is the shared Python library for the Ovid harness family. It is intended to centralize configuration management, provider setup, default tools, and customizable public interfaces for consumers such as `ovid-code` and `ovid-one`. Optional server support exposes the core over HTTP and stdio transports.

The repository is currently a package scaffold: `src/ovid_core/__init__.py` is empty and no runtime modules, entry points, or server implementation exist yet. Keep documentation and changes explicit about implemented behavior versus this target architecture.

## Architecture & Data Flow

Target flow: consumer configuration → core configuration models → provider/tool factories → agent-facing public interfaces → optional HTTP/stdio server adapters. Keep reusable domain and orchestration code transport-independent; server modules should adapt the same public interfaces rather than duplicate behavior.

No dependency-injection container or global state pattern exists. Prefer explicit constructor/factory parameters and typed interfaces as the package grows. Keep optional server imports out of base-package import paths so `import ovid_core` works without `ovid-core[server]`.

## Key Directories

- `src/ovid_core/`: installable `src`-layout package. Add configuration, providers, tools, interfaces, and transport adapters here as focused modules.
- `src/tests/`: placeholder test directory used by the current `task tests` command; it is empty.
- `vulture/whitelist.txt`: intentional dead-code exceptions for Vulture; currently empty.
- `dist/`: generated wheel and source distributions; do not edit generated artifacts.

## Development Commands

Use uv from the repository root:

```bash
uv sync                         # create/update the development environment
uv sync --extra server          # include optional MCP server dependencies
uv build                        # build wheel and sdist
uv run task ruff                # format and autofix source and tests
uv run task ruff-lint           # check package lint only
uv run task ty-lint             # type-check src/ovid_core
uv run task vulture             # detect unused code
uv run task format-and-lint     # run Ruff, then ty
uv run task tests               # configured pytest command for src/tests
```

`uv.lock` is generated dependency state. Update it with uv after changing `pyproject.toml`; do not hand-edit it.

## Code Conventions & Common Patterns

- Target current Python 3.14 conventions. The package is fully typed (`src/ovid_core/py.typed`); Ruff enforces annotations (`ANN`) and modern syntax (`UP`).
- Ruff formatting is authoritative: 120-column lines, four-space indentation, single quotes, and sorted imports.
- Use `snake_case` for modules/functions/variables, `PascalCase` for types, and explicit names for provider/tool factories.
- Prefer keyword arguments at call sites when passing multiple values: `func(a=1, b=2, c=3)`, not `func(1, 2, 3)`.
- Annotate every function parameter and return type. Parameterize every generic (`list[str]`, `dict[str, Any]`); never use bare containers or `object` in annotations. Prefer a precise type, otherwise use `Any`.
- Use Python 3.14 generic syntax, such as `def foo[**P, T](...)` and `class Container[T]:`; do not introduce legacy `TypeVar` or `ParamSpec` declarations.
- Never silence ty or another type checker. Fix the type model or report the blocker. Existing suppressions outside the changed code must remain untouched.
- Use f-strings only. Do not use `.format()`, `%` interpolation, or string concatenation for templating.
- Do not use local imports unless they are required to break an unavoidable import cycle. Prefer resolving the cycle structurally.
- Do not define `__all__` and do not re-export symbols from `__init__.py`; consumers must import from the module that owns a symbol.
- Functions should stay within 40 lines and files within 250 lines. Split by responsibility before exceeding either limit; rare exceptions must be inherently indivisible.
- Follow DRY strictly. Extract shared behavior instead of repeating code, schemas, validation, constants, or control flow.
- Do not add comments, docstrings, or explanatory prose unless the behavior is critical and genuinely non-obvious, or the user explicitly requests documentation. Prefer clear names and smaller units.
- Do not return unstructured `dict` values or `list[dict[...]]` from functions. Represent complex results with typed Pydantic `BaseModel` DTOs.
- Keep the public surface deliberate. Stable interfaces live in their owning modules; implementation details stay private.
- Raise narrow typed exceptions, preserve causes with `raise ... from ...`, and translate errors only at provider or transport boundaries.
- Use async for provider, tool, and HTTP/stdio I/O; never perform blocking work in async paths.
- Prefer dependency injection through typed constructor or factory parameters. Avoid module-level mutable state, service locators, and hidden singleton configuration.
- Keep base functionality independent of optional dependencies. Server-only code must be import-safe when the `server` extra is absent.

## Important Files

- `pyproject.toml`: package metadata, dependencies/extras, uv build backend, task commands, and all Ruff/ty/pytest/coverage/Vulture policy.
- `uv.lock`: resolved runtime, optional, and development dependency graph.
- `.python-version`: pins local Python to 3.14.
- `src/ovid_core/__init__.py`: future stable public package surface; currently empty.
- `src/ovid_core/py.typed`: marks the distribution as typed.
- `AGENTS.md`: repository-specific guidance; update it when architecture or commands become concrete.

## Runtime/Tooling Preferences

- Runtime: CPython `>=3.14`; local version is `3.14`.
- Package/environment manager: uv. Build backend: `uv_build`.
- Task runner: taskipy through `uv run task ...`.
- Formatting/linting: Ruff. Type checking: ty. Dead-code analysis: Vulture.
- Base AI dependency: `pydantic-ai-slim` with supported provider extras. MCP HTTP/stdio server dependencies belong to the optional `server` extra, installable as `ovid-core[server]`.
- There are no console scripts or CI workflows yet. Do not document or depend on an executable entry point until `[project.scripts]` is added.

## Testing & QA

Pytest is configured for `test*.py`, strict markers/config, short tracebacks, automatic asyncio mode, branch coverage, HTML coverage output, and a 100% coverage threshold. New observable behavior should include focused tests covering boundaries, failures, and async transitions.

Current QA metadata has known drift:

- `uv run task tests` targets `src/tests/`, while pytest `testpaths` points to absent root `tests/`.
- pytest options require `pytest-cov`, `pytest-asyncio`, and `pytest-socket`, but those plugins are not declared in the development dependency group.
- Ruff `known-first-party` is `app`, not `ovid_core`.

Reconcile these settings before establishing the permanent test layout. Do not create a second test convention alongside the existing configuration.

## Validation Checklist

Run these checks after implementation and before reporting work complete:

```bash
uv run task ty-lint
uv run ruff format --check ./src/ovid_core ./src/tests/
uv run ruff check ./src/ovid_core ./src/tests/
uv run task tests
```

All checks must pass. If existing QA configuration prevents execution, identify the repository-owned blocker explicitly rather than skipping or weakening a check.
