# Repository Guidelines

## Project

`ovid-core` is the shared Python library for Ovid applications. It owns stable contracts for configuration, model routing, agents, messages, results, usage, tools, capabilities, storage, and transports.

Pydantic AI runs model calls, the agent loop, tools, streaming, and provider integrations. Ovid Core wraps that runtime with Ovid-owned types. It does not fork or replace Pydantic AI.

Installing this package does not start a server or add tools to an agent. Applications enable each optional feature.

## Architecture and dependency rules

Use this dependency direction:

```text
Ovid application
      |
      v
ovid_core public contracts
      |
      v
ovid_core.adapters.pydantic_ai
      |
      v
Pydantic AI and provider SDKs
```

Place code according to ownership:

- Put stable application-facing values and protocols in their domain module under `ovid_core`.
- Put Pydantic AI and provider runtime types only in `ovid_core.adapters.pydantic_ai`.
- Put Starlette-specific delivery code in `ovid_core.adapters.starlette`.
- Put transport-neutral server contracts in `ovid_core.server`.
- Keep configuration loading independent of transports, providers, and application source discovery.
- Keep application policy in the application. This includes config precedence, secret discovery, prompts, tool selection, authorization, storage policy, deployment, and UI behavior.
- Keep `ovid-native` optional. `ovid-core` must never import or depend on it.

Adapters translate values at the boundary. They must not define a second copy of a domain contract. Pass dependencies through typed parameters; do not add service locators, hidden singletons, or mutable global configuration.

## Folder structure

The repository follows this layout:

```text
src/
├── ovid_core/
│   ├── <domain>/                  Domain models, protocols, and behavior
│   ├── adapters/
│   │   ├── pydantic_ai/          Pydantic AI compatibility boundary
│   │   └── starlette/            Starlette delivery code
│   ├── server/                   Transport-neutral server contracts
│   ├── agents.py                 Agent construction and runtime facade
│   ├── models.py                 Shared Pydantic model bases
│   └── __init__.py               Deliberate high-level public exports
└── tests/
    ├── <domain>/                 Domain tests
    └── test_*.py                 Cross-domain contract tests
docs/content/                     User documentation
vulture/whitelist.txt             Intentional public code reported as unused
pyproject.toml                    Package, task, lint, type, and test settings
uv.lock                           Generated dependency lock
```

A domain should keep related code together. For example:

```text
src/ovid_core/relay/
├── models.py
├── contracts.py
├── connection.py
├── capability.py
├── tools.py
└── errors.py
```

Do not create generic `schemas`, `dto`, `interfaces`, `utils`, or `helpers` packages. Add a shared module only after more than one domain needs the same behavior.

Generated files belong in `.venv/`, `dist/`, coverage output, or caches. Do not edit or commit them.

## Development commands

Run commands from the repository root:

```bash
uv sync
uv build
uv run task ruff
uv run task ruff-lint
uv run task ty-lint
uv run task vulture
uv run task tests
uv run task docs-build
```

Use `uv run task docs-run` to serve the documentation locally.

`uv.lock` is generated. Update it with uv after changing dependencies; never edit it by hand.

## Python rules

### Types and models

- Target Python 3.14. Use current generic syntax instead of `TypeVar` or `ParamSpec` declarations.
- Annotate every parameter and return value. Parameterize every collection.
- Use a precise type when possible. Use `Any` when the value is truly dynamic; do not use `object` as a substitute.
- Do not silence ty. Fix the type model or report the blocker.
- Public structured DTOs inherit `ovid_core.models.BaseModel`. UUID-like root values inherit `BaseRootModel`.
- Use Pydantic models for validated data. Use plain classes for factories, routers, resolvers, clients, stores, and other stateful services.
- Return typed models for complex results. Do not return unstructured dictionaries or lists of dictionaries.

### Naming and imports

- Use `snake_case` for modules, functions, and variables. Use `PascalCase` for types.
- Ruff controls formatting: 120-column lines, four spaces, single quotes, and sorted imports.
- Use keyword arguments when a call passes several values.
- Use f-strings for interpolation. Do not use `.format()`, `%` formatting, or string concatenation for templates.
- Avoid local imports. Break import cycles by changing module boundaries first.
- Keep public exports deliberate. `ovid_core.__init__` exposes only stable high-level contracts; domain modules own the full API.

### Boundaries and safety

- Domain modules use Ovid-owned values. Pydantic AI and provider runtime types stay inside adapters.
- Map validated fields directly at adapter boundaries. Do not add parallel wrapper hierarchies without a current contract.
- Every Ovid-owned structured payload is a typed model, including persisted records and private HTTP bodies.
- Serialize JSON request models once with `model_dump_json()` and pass them through HTTPX `content=` with the JSON content type. Use `data=` only for explicit form requests.
- Keep imports free of network calls, authentication, file discovery, and other environment-dependent work.
- Use async for provider, tool, HTTP, and stdio I/O. Do not block the event loop.
- Raise narrow exceptions and preserve causes. Remove a cause only when it could expose a secret, header, credential, or signed URL.
- Never place resolved credentials in configuration, DTOs, logs, exceptions, or serialized data. Resolvers return `SecretStr`.

### Code shape

- Keep functions within 40 lines and files within 250 lines unless the behavior cannot be split cleanly.
- Use guard clauses and shallow indentation.
- Separate setup, decisions, side effects, and return values with blank lines.
- Add an abstraction only when it removes real duplication or isolates a required boundary.
- Delete speculative options, wrappers, branches, and extension points.
- Add comments or docstrings only when a critical rule is not clear from names and types.

## Domain contracts

### Configuration and credentials

- Core accepts one final `OvidConfig`.
- Applications own config file discovery, environment mapping, precedence, merging, and profile selection.
- Run persisted configuration through `migrate_config` before validation. Applications provide migrations explicitly.
- Configuration stores credential references, not secret values.

### Models and routing

- Store `provider` and `model` separately. Join them only in the Pydantic AI model adapter.
- `known_models()` may expose the upstream catalog, but unknown future provider/model pairs remain valid until construction.
- Provider retries finish inside one route candidate. Ovid fallback moves to the next candidate only after an eligible final failure.
- Concurrency limits wrap each candidate before fallback is compiled.

### Capabilities, MCP, and Skills

- Add provider behavior only through explicit capabilities. Do not imitate provider-native features with Ovid tools.
- MCP configuration uses typed stdio or HTTP definitions and credential references. Pydantic AI and FastMCP own transport lifecycles.
- Agent Skills use the official `pydantic-ai-harness` `Skills` capability with explicit trusted directories.
- Do not search for skills implicitly, parse `SKILL.md` separately, execute skill scripts, or hide filesystem trust policy in core.

### Codex subscription

- Keep ChatGPT Codex authentication inside `CodexSubscriptionModelFactory` and `ovid_core.codex`.
- Use the official device flow, token rotation, model catalog, and dedicated authenticated client.
- Do not copy Codex prompts, serialize OAuth tokens, claim another client identity, or silently fall back to API-key billing.
- Treat the ChatGPT backend as an undocumented contract and keep it isolated from normal provider setup.

### Usage and observability

- Normalize all usage reported by Pydantic AI. Do not invent usage for failed attempts that report none.
- Child usage trackers keep a local ledger and forward each delta to the parent once.
- Check aggregate limits before model calls and tool calls, and after usage updates.
- Pydantic AI owns OpenTelemetry instrumentation. Core maps `ObservabilityConfig` to upstream settings.
- Keep prompt and completion content disabled unless the application sets `include_content=True`.

### Servers and persistence

- Server factories accept constructed, registered agents and explicit authorization and dependency callbacks.
- Server conversation history is authoritative. Client prompts, approvals, tools, files, and protocol state do not grant permission.
- Native HTTP and SSE use Ovid events. AG-UI delegates protocol work to Pydantic AI. Stdio uses versioned newline-delimited JSON and Ovid events.
- Core defines no built-in commands, slash syntax, prompts, agent catalog, ACP, coding RPC, or UI.
- Applications own durable storage and session policy through the conversation store protocol.

## Testing and validation

Tests must protect observable contracts: success, failure, boundaries, serialization, cancellation, cleanup, and async transitions.

Use the `mocker: MockerFixture` fixture for every double, patch, spy, or environment change. Do not use `unittest.mock`, `monkeypatch`, or another mocking helper.

Before reporting work complete, run:

```bash
uv run task ty-lint
uv run ruff format --check ./src/ovid_core ./src/tests
uv run ruff check ./src/ovid_core ./src/tests
uv run task vulture
uv run task tests
uv run task docs-build
uv build
```

The Python integration layer requires 100% branch coverage. Do not weaken checks or thresholds. If a repository problem blocks a check, report the exact blocker.
