# Public API overview

This section is the exact interface reference. Start with [Why Ovid Core](../why-ovid-core.md) for the design motivation, [Architecture](../architecture.md) for what happens underneath, or the [guides](../getting-started.md) for task-oriented integration.

| Area | Modules | Reference |
| --- | --- | --- |
| Shared immutable Pydantic bases | `ovid_core.models` | [Models and configuration](configuration.md) |
| Configuration models, validation, loading, migrations | `ovid_core.config.models`, `.loading`, `.errors` | [Models and configuration](configuration.md) |
| Serializable credential references and resolution | `ovid_core.credentials.models`, `.resolvers` | [Credentials](credentials.md) |
| Model selectors, handles, factories, and routing | `ovid_core.routing.models`, `.factory`, `.router` | [Routing and agents](agents.md) |
| Agent definitions, construction, runtime, and streaming | `ovid_core.agents` | [Routing and agents](agents.md) |
| Normalized messages, identities, contexts, events, and results | `ovid_core.messages.models`, `ovid_core.runtime.*` | [Messages and runtime](runtime.md) |
| Usage, limits, retries, and instrumentation policy | `ovid_core.usage.*`, `ovid_core.policy`, `ovid_core.observability` | [Usage, policy, and observability](usage-policy.md) |
| Tools, toolsets, hooks, capabilities, skills, and MCP | `ovid_core.tools.*`, `ovid_core.hooks.base`, `ovid_core.capabilities.*`, `ovid_core.skills`, `ovid_core.mcp.*` | [Tools and capabilities](extensions.md) |
| Conversation persistence | `ovid_core.persistence` | [Persistence](persistence.md) |
| ChatGPT Codex subscription authentication and models | `ovid_core.codex.*`, `ovid_core.adapters.pydantic_ai.codex` | [Codex subscription](codex.md) |
| Native HTTP, SSE, stdio, and AG-UI servers | `ovid_core.server.*` | [Servers and transports](server.md) |
| Pydantic AI and Starlette compatibility boundaries | `ovid_core.adapters.pydantic_ai.*`, `ovid_core.adapters.starlette.*` | [Pydantic AI adapters](adapters.md) |
| Exception hierarchy | `ovid_core.errors`, `ovid_core.config.errors` | [Errors](errors.md) |

## API conventions

- All Ovid Pydantic DTOs inherit `ovid_core.models.BaseModel`.
- Ovid models are immutable and reject extra fields.
- Root identifier values inherit `BaseRootModel`.
- Generic runtime contracts use Python 3.14 type parameters such as `AgentDefinition[Deps, Output]`.
- Protocols describe application implementations. Structural typing is sufficient. Inheritance is optional.
- Async methods perform provider, credential, storage, tool, or transport I/O.
- Discriminated unions use the `kind` or `type` field for JSON validation.
- `JsonValue` means a JSON-compatible scalar, list, or mapping accepted by Pydantic.
- Defaults shown in this reference are part of the current public contract.

## Pydantic model operations

Each Ovid model supports the normal Pydantic operations.

These operations include `model_validate`, `model_validate_json`, `model_dump`, `model_dump_json`, and `model_copy`. Ovid models reject changes after construction.
