# Repository Guidelines

## Project Overview

`ovid-core` is the shared Python library for the Ovid harness family. It owns stable Ovid-facing runtime values, typed domain-neutral configuration, generic model selection, and the compatibility boundary around Pydantic AI. Tools, plugins, and optional transports build on those contracts without exposing upstream runtime objects to consumers.

The package includes stable runtime contracts, typed configuration and credential references, model routing, ChatGPT Codex subscription authentication, Ovid-owned tool/toolset/capability/hook contracts, the typed agent factory and runtime facade, nested usage accounting, run policy, observability, and their Pydantic AI adapters. `src/ovid_core/__init__.py` intentionally remains empty; consumers import from the module that owns each symbol.

## Architecture & Data Flow

Flow: final typed configuration → credential or subscription authentication → Pydantic AI model inference → generic model routing → explicit agent construction → run policy and aggregate usage enforcement → Pydantic AI instrumentation → optional transport adapters. Configuration and domain packages are transport-independent. `ovid_core.adapters` is the only boundary that translates or constructs third-party runtime values.

Organize by domain, not by type or release stage. A domain package owns its models and behavior; do not duplicate the same value in a generic `schemas`, `dto`, or `interfaces` package. Adapters may translate values but must not redefine domain contracts. No dependency-injection container or global state pattern exists; use explicit typed parameters.

## Key Directories

- `src/ovid_core/models.py`: shared immutable, extra-forbidding Pydantic model bases. Domain models inherit these instead of repeating configuration.
- `src/ovid_core/config/`: typed configuration sections, explicit schema migrations, JSON/TOML loading, and source-safe validation errors. Consumers own source discovery, environment mapping, precedence, merging, and profiles.
- `src/ovid_core/credentials/`: serializable credential references and resolver contracts. Resolved values use `SecretStr` and never enter configuration.
- `src/ovid_core/codex/`: OpenAI Codex device authorization, token refresh, opaque token values, token-store contracts, and optional system-keyring persistence.
- `src/ovid_core/routing/`: generic model factory and opaque handle contracts plus exact model, alias, candidate, and route resolution.
- `src/ovid_core/usage/`: request, run, and nested subagent usage accounting owned by core.
- `src/ovid_core/messages/`: normalized conversation message values.
- `src/ovid_core/runtime/`: run identities, contexts, events, and results.
- `src/ovid_core/tools/`: typed tool arguments/results, execution context, and tool/toolset lifecycle contracts.
- `src/ovid_core/capabilities/`: opt-in instruction, tool, toolset, hook, and model-setting contributions.
- `src/ovid_core/hooks/`: Ovid-owned lifecycle hooks limited to currently implemented tool execution points.
- `src/ovid_core/agents.py`: explicit typed agent construction, diagnostics, and Ovid-owned run/stream facade contracts.
- `src/ovid_core/policy.py`: retry, limit, timeout, concurrency, and fallback classification contracts; cancellation always propagates.
- `src/ovid_core/observability.py`: the stable configuration boundary for Pydantic AI instrumentation.
- `src/ovid_core/adapters/pydantic_ai/`: private compatibility translation, runtime policy enforcement, usage tracking, and upstream instrumentation configuration for the supported Pydantic AI range.
- `src/tests/`: contract, compatibility, and serialization tests matching the package domains.
- `vulture/whitelist.txt`: intentional public contracts not yet referenced by runtime code.
- `dist/`: generated wheel and source distributions; do not edit generated artifacts.

## Development Commands

Use uv from the repository root:

```bash
uv sync                         # create/update the development environment
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
- Use Pydantic models for validated data, not for stateful services such as factories, routers, resolvers, clients, or stores. Keep those as plain classes, and do not add `__slots__` without a measured memory requirement.
- Keep Pydantic AI imports under `ovid_core.adapters.pydantic_ai`. Domain packages must depend only on Ovid-owned values.
- Prefer direct validated field mapping at adapter boundaries. Do not build parallel wrapper hierarchies when a stable scalar field or namespaced mapping preserves the required semantics.
- Every Ovid-owned structured payload, including private HTTP request bodies and persisted records, must be a typed `BaseModel`; never assemble protocol payloads as inline dictionaries. Dynamic third-party pass-through bodies are the exception and must be validated through a typed adapter at the boundary.
- For JSON requests, serialize the request model once with `model_dump_json()`, pass the serialized body directly through HTTPX `content=`, and supply `Content-Type: application/json`; do not use HTTPX `json=` or manually call `json.dumps`. HTTPX `data=` only accepts mappings in its typed 0.28 API and is reserved here for form requests using `model_dump(mode='json')` with the explicit form content type.
- Core accepts one final `OvidConfig`. Consuming applications own source discovery, environment mapping, precedence, merging, and profile selection; do not recreate those application policies in this library.
- Persisted configuration passes through `migrate_config` before validation. Every schema migration is explicitly supplied by the embedding application; never mutate source mappings or add an implicit compatibility fallback.
- Credentials in configuration are references only. Resolver implementations return `SecretStr`; never include resolved values in DTOs, exceptions, logs, or serialization.
- Model configuration stores `provider` and `model` separately. `PydanticAIModelFactory` joins them only at the adapter boundary for `infer_model`; do not reproduce Pydantic AI's provider list, constructors, or SDK-specific configuration models in core.
- `known_models()` delegates to Pydantic AI's public catalog and returns typed provider/model pairs. Unknown future pairs remain valid configuration and are checked at construction time.
- Pydantic AI models and providers normally own their HTTP-client lifecycle. The Codex subscription model is the narrow exception: it owns and closes the authenticated OpenAI client it constructs for the undocumented ChatGPT backend.
- Provider SDK retries complete inside one candidate attempt. Pydantic AI fallback then advances through the configured candidate order after a fallback-eligible terminal failure. Concurrency limits wrap each candidate before fallback compilation.
- The default model factory follows Pydantic AI provider authentication and environment conventions. Pydantic AI supports API keys and provider-native mechanisms such as cloud credential chains, ADC, profiles, and custom SDK clients where each provider implements them.
- `CodexSubscriptionModelFactory` uses OpenAI Codex's device OAuth endpoints, refreshes rotating tokens, stores them through `CodexTokenStore`, and gives Pydantic AI's `OpenAIResponsesModel` the ChatGPT Codex base URL and authenticated HTTP client directly. It loads the authenticated Codex `/models` instruction catalog once per factory, preserves the selected model's validated base instructions as top-level Responses instructions, and maps consumer instructions to developer input. Otherwise do not rewrite Pydantic AI request bodies; core only enforces `store=false`, streaming requests, encrypted reasoning replay, and the required Codex authentication headers.
- Codex subscription access relies on an undocumented backend contract even though the device flow and model catalog are implemented by the official Apache-licensed Codex CLI. Keep it isolated behind the dedicated factory; never hard-code a copied Codex prompt, put OAuth tokens in generic settings, serialize them, claim another client as the request originator, or silently fall back to API-key billing.
- Usage normalization includes every request reported by Pydantic AI. A failed provider attempt that exposes no upstream usage cannot be counted; successful fallback responses and later agent-loop requests aggregate through the core `Usage` contract. `UsageTracker.create_child()` gives each subagent a local ledger while forwarding usage deltas exactly once to its parent. Aggregate limits are checked before model requests and tool calls and after usage updates, including nested runs, while each `RunResult` retains only that run's usage.
- Pydantic AI owns OpenTelemetry instrumentation. Core only maps `ObservabilityConfig` to Pydantic AI's public `InstrumentationSettings`; applications configure their preferred OpenTelemetry or Logfire export path outside core. Prompt, completion, binary, and model-request-parameter content remains excluded unless `include_content=True` is explicitly selected.
- OpenAI, HTTPX, and system-keyring support are required runtime dependencies. `pydantic-ai-slim[openai]` supplies the compatible OpenAI SDK; do not add a duplicate direct OpenAI version constraint. Prefer Pydantic AI provider APIs over constructing SDK clients, and import SDK wire types only inside the Pydantic AI adapter when no public Pydantic AI type exists. Keep concrete integration objects inside provider, authentication, or adapter boundaries rather than leaking them into domain contracts.
- Minimize code as a primary design constraint. Implement only behavior required by a current contract, prefer direct data flow and upstream or standard-library capabilities, and delete speculative options, wrappers, helpers, branches, and extension points. Add an abstraction only when it removes demonstrated duplication or isolates a required boundary; fewer readable lines are better than a more flexible design.
- Functions should stay within 40 lines and files within 250 lines. Split by responsibility before exceeding either limit; rare exceptions must be inherently indivisible.
- Prefer guard clauses when the alternative path exits the function; handle absence or failure first, return, then leave the main operation unnested.
- Follow DRY strictly. Extract shared behavior instead of repeating code, schemas, validation, constants, or control flow.
- Do not add comments, docstrings, or explanatory prose unless the behavior is critical and genuinely non-obvious, or the user explicitly requests documentation. Prefer clear names and smaller units.
- Do not return unstructured `dict` values or `list[dict[...]]` from functions. Represent complex results with typed Pydantic `BaseModel` DTOs.
- Keep the public surface deliberate. Stable interfaces live in their owning modules; implementation details stay private.
- Raise narrow typed exceptions and preserve causes unless an adapter boundary must deliberately discard a cause to prevent credential, header, or signed-URL disclosure.
- Use async for provider, tool, and HTTP/stdio I/O; never perform blocking work in async paths.
- Prefer dependency injection through typed constructor or factory parameters. Avoid module-level mutable state, service locators, and hidden singleton configuration.
- All built-in integration dependencies are installed with the package. Keep import paths free of environment-dependent side effects; authentication and network access occur only through explicit runtime calls.

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
- `src/ovid_core/runtime/`, `messages/`, and `usage/`: stable runtime values and nested usage tracking.
- `src/ovid_core/config/` and `credentials/`: final configuration and secret-reference contracts.
- `src/ovid_core/codex/`: optional Codex subscription authentication and system-keyring persistence.
- `src/ovid_core/routing/`: generic model factories, handles, and selection contracts.
- `src/ovid_core/agents.py`: typed agent factory, construction diagnostics, and run/stream facade.
- `src/ovid_core/policy.py` and `observability.py`: run policy and Pydantic AI instrumentation configuration.
- `src/ovid_core/adapters/pydantic_ai/`: upstream model, agent, tool, message, event, usage, result, fallback, concurrency, policy, and instrumentation implementations.
- `src/ovid_core/__init__.py`: intentionally empty; no package-level re-exports.
- `src/ovid_core/py.typed`: marks the distribution as typed.
- `AGENTS.md`: repository-specific architecture and engineering guidance.

## Runtime/Tooling Preferences

- Runtime: CPython `>=3.14`; local version is `3.14`.
- Package/environment manager: uv. Build backend: `uv_build`.
- Task runner: taskipy through `uv run task ...`.
- Formatting/linting: Ruff. Type checking: ty. Dead-code analysis: Vulture.
- Runtime dependencies include `pydantic-ai-slim[openai]>=2.22,<2.23`, `httpx>=0.28.1,<0.29`, and `keyring>=25.6,<26`; Codex subscription authentication must work in a normal installation without extras.
- Canonical upstream references: https://pydantic.dev/docs/ai/overview/ and https://github.com/openai/codex/tree/main/sdk/python. Consult them before changing upstream integration or authentication behavior.
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
