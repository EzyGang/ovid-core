# Repository Guidelines

## Project Overview

`ovid-core` is the shared Python library for the Ovid harness family. It owns stable Ovid-facing runtime values and the compatibility boundary around Pydantic AI. Configuration, providers, tools, plugins, and optional transports will build on those contracts without exposing upstream runtime objects to consumers.

Phase 1 is implemented: stable errors, identities, usage, messages, events, contexts, results, and Pydantic AI adapters. `src/ovid_core/__init__.py` intentionally remains empty; consumers import from the module that owns each symbol.

## Architecture & Data Flow

Flow: consumer configuration → core domain contracts → provider/tool factories → agent-facing interfaces → optional transport adapters. Domain packages are transport- and provider-independent. `ovid_core.adapters` is the only layer that translates third-party runtime values.

Organize by domain, not by type or implementation phase. A domain package owns its models and behavior; do not duplicate the same value in a generic `schemas`, `dto`, or `interfaces` package. Adapters may translate values but must not redefine domain contracts. No dependency-injection container or global state pattern exists; use explicit typed parameters.

## Key Directories

- `src/ovid_core/models.py`: shared immutable, extra-forbidding Pydantic model bases. Domain models inherit these instead of repeating configuration.
- `src/ovid_core/usage/`: request and aggregate usage accounting owned by core.
- `src/ovid_core/messages/`: normalized conversation message values.
- `src/ovid_core/runtime/`: run identities, contexts, events, and results.
- `src/ovid_core/adapters/pydantic_ai/`: private compatibility translation from/to the supported Pydantic AI range.
- `src/tests/`: contract, compatibility, and serialization tests matching the package domains.
- `vulture/whitelist.txt`: intentional public contracts not yet referenced by later runtime phases.
- `dist/`: generated wheel and source distributions; do not edit generated artifacts.

## Development Commands

Use uv from the repository root:

```bash
uv sync                         # create/update the development environment
uv sync --extra openai          # include the first declared provider SDK extra
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
- All Pydantic DTOs inherit `ovid_core.models.BaseModel`; UUID-like root values inherit `BaseRootModel`. Do not repeat `ConfigDict(extra='forbid', frozen=True)`.
- Keep Pydantic AI imports under `ovid_core.adapters.pydantic_ai`. Domain packages must depend only on Ovid-owned values.
- Prefer direct validated field mapping at adapter boundaries. Do not build parallel wrapper hierarchies when a stable scalar field or namespaced mapping preserves the required semantics.
- Functions should stay within 40 lines and files within 250 lines. Split by responsibility before exceeding either limit; rare exceptions must be inherently indivisible.
- Follow DRY strictly. Extract shared behavior instead of repeating code, schemas, validation, constants, or control flow.
- Do not add comments, docstrings, or explanatory prose unless the behavior is critical and genuinely non-obvious, or the user explicitly requests documentation. Prefer clear names and smaller units.
- Do not return unstructured `dict` values or `list[dict[...]]` from functions. Represent complex results with typed Pydantic `BaseModel` DTOs.
- Keep the public surface deliberate. Stable interfaces live in their owning modules; implementation details stay private.
- Raise narrow typed exceptions, preserve causes with `raise ... from ...`, and translate errors only at provider or transport boundaries.
- Use async for provider, tool, and HTTP/stdio I/O; never perform blocking work in async paths.
- Prefer dependency injection through typed constructor or factory parameters. Avoid module-level mutable state, service locators, and hidden singleton configuration.
- Keep base functionality independent of optional dependencies. Server-only code must be import-safe when the `server` extra is absent.

### Line breaks in code
Here is a bad example:
```python

def reject_secret_keys(self) -> Self:
    normalized_key = self.key.casefold().replace('-', '_')
    if any(secret_key in normalized_key for secret_key in _SECRET_METADATA_KEYS):
        raise ValueError('result metadata keys cannot identify secret values')
    return self
```

And here is a good example:
```python
def reject_secret_keys(self) -> Self:
    normalized_key = self.key.casefold().replace('-', '_')
    
    if any(secret_key in normalized_key for secret_key in _SECRET_METADATA_KEYS):
        raise ValueError('result metadata keys cannot identify secret values')
        
    return self
```

Meaning that code line breaks should split logical groups of code, i.e. inputs, logic blocks, returns.

## Important Files

- `pyproject.toml`: package metadata, dependencies/extras, uv build backend, task commands, and all Ruff/ty/pytest/coverage/Vulture policy.
- `uv.lock`: resolved runtime, optional, and development dependency graph.
- `.python-version`: pins local Python to 3.14.
- `src/ovid_core/models.py`: shared Pydantic model configuration.
- `src/ovid_core/runtime/`, `messages/`, and `usage/`: implemented stable Phase 1 contracts.
- `src/ovid_core/adapters/pydantic_ai/`: supported upstream compatibility boundary.
- `src/ovid_core/__init__.py`: intentionally empty; no package-level re-exports.
- `src/ovid_core/py.typed`: marks the distribution as typed.
- `AGENTS.md`: repository-specific architecture and engineering guidance.

## Runtime/Tooling Preferences

- Runtime: CPython `>=3.14`; local version is `3.14`.
- Package/environment manager: uv. Build backend: `uv_build`.
- Task runner: taskipy through `uv run task ...`.
- Formatting/linting: Ruff. Type checking: ty. Dead-code analysis: Vulture.
- Base AI dependency: `pydantic-ai-slim>=2.22,<2.23`. Provider SDKs are optional; the currently declared provider extra is `ovid-core[openai]`.
- There are no console scripts or CI workflows yet. Do not document or depend on an executable entry point until `[project.scripts]` is added.

## Testing & QA

Pytest is configured for `test*.py`, strict markers/config, short tracebacks, automatic asyncio mode, branch coverage, HTML coverage output, and a 100% coverage threshold. New observable behavior should include focused tests covering boundaries, failures, and async transitions.

Pytest discovery and the task command both use `src/tests/`. Required pytest plugins are declared in the development dependency group, and Ruff recognizes `ovid_core` as first-party.

## Validation Checklist

Run these checks after implementation and before reporting work complete:

```bash
uv run task ty-lint
uv run ruff format --check ./src/ovid_core ./src/tests/
uv run ruff check ./src/ovid_core ./src/tests/
uv run task vulture
uv run task tests
```

All checks must pass. If existing QA configuration prevents execution, identify the repository-owned blocker explicitly rather than skipping or weakening a check.
