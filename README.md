<h1 align="center">Ovid Core</h1>

<p align="center">
  Typed building blocks for Python agent applications.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.14%2B-blue" alt="Python 3.14 or newer"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-0.1.0-indigo" alt="Version 0.1.0"></a>
  <a href="https://github.com/EzyGang/ovid-core/actions/workflows/ci.yml"><img src="https://github.com/EzyGang/ovid-core/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license"></a>
</p>

---

**Ovid Core is built on top of [Pydantic AI](https://ai.pydantic.dev/).** Pydantic AI runs the model, agent loop, tools, structured output, streaming, and provider integrations.

Ovid Core adds a typed application layer around that runtime. It gives the rest of your application one set of contracts for configuration, messages, results, usage, tools, storage, and transports. It does not replace or fork Pydantic AI.

Installing Ovid Core does not start a server or add tools to an agent. Your application chooses every optional feature.

**Documentation:** [docs/content/index.md](docs/content/index.md)  
**Source code:** https://github.com/EzyGang/ovid-core  
**Native tools:** https://github.com/EzyGang/ovid-native

---

## Table of contents

- [Why Ovid Core?](#why-ovid-core)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Core features](#core-features)
- [Pydantic AI and Harness compatibility](#pydantic-ai-and-harness-compatibility)
- [Optional server support](#optional-server-support)
- [Application ownership](#application-ownership)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

---

## Why Ovid Core?

Use Pydantic AI directly when one small process owns the agent. It is the simpler choice when upstream types can stay inside that process.

Use Ovid Core when the agent is a shared application component. A worker, service, database, command-line program, and user interface can then use the same agent definition and data contracts.

```mermaid
graph LR
    APP[Your application] --> CORE[Ovid Core contracts]
    CORE --> PAI[Pydantic AI runtime]
    PAI --> PROVIDER[Model provider]
```

Ovid Core adds the boundary between application code and the Pydantic AI runtime:

| Need | Direct Pydantic AI | Ovid Core |
| --- | --- | --- |
| Model selection | Use a model string or provider object | Use configured names, aliases, routes, and fallbacks |
| Messages and results | Use Pydantic AI runtime values | Use frozen Ovid models |
| Tools | Use Pydantic AI tools and toolsets | Use typed Ovid tools, approval data, timeouts, and hooks |
| Usage limits | Limit one upstream run | Share one budget across parent and child agents |
| Storage | Store upstream messages or convert them yourself | Use normalized messages and a versioned codec |
| Transports | Build an application transport | Use optional HTTP, SSE, stdio, and AG-UI adapters |
| Upstream changes | Update each caller | Update the Ovid adapter boundary |

This extra layer has a cost. Do not add Ovid Core to a small script that does not need these contracts. Add it when agent data must cross process, storage, transport, or team boundaries.

Read [Why Ovid Core](docs/content/why-ovid-core.md) for the full comparison.

---

## Installation

Ovid Core requires Python 3.14 or newer.

### uv

```bash
uv add ovid-core
```

### pip

```bash
pip install ovid-core
```

Add server support only when you need it:

```bash
uv add 'ovid-core[server]'
```

Add AG-UI support with:

```bash
uv add 'ovid-core[server-ag-ui]'
```

---

## Quick start

Create `ovid.toml`:

```toml
[models.primary]
provider = "openai"
model = "gpt-5"
```

Set the provider key:

```bash
export OPENAI_API_KEY='...'
```

On Windows PowerShell:

```powershell
$env:OPENAI_API_KEY = '...'
```

Create and run the agent:

```python
import asyncio
from pathlib import Path

from ovid_core import AgentDefinition, AgentFactory
from ovid_core.config import load_config_file
from ovid_core.routing import ModelRef


async def main() -> None:
    config = load_config_file(Path('ovid.toml'))
    factory = AgentFactory(config=config)
    agent = await factory.build(
        AgentDefinition[None, str](
            model=ModelRef(name='primary'),
            deps_type=type(None),
            output_type=str,
            instructions=('Answer clearly. Do not invent facts.',),
        )
    )

    result = await agent.run('What is 2 + 2?', deps=None)

    print(result.output)
    print(result.usage)
    print(result.run_id)


asyncio.run(main())
```

The result uses Ovid types. It contains the validated output, messages, usage, run ID, and conversation ID.

Read the [getting started guide](docs/content/getting-started.md) for provider keys, routes, streaming, and error handling.

---

## Core features

### Configuration and model routes

Give models local names such as `primary` or `fast`. Agent definitions use these names instead of provider strings. A route can try another model after an eligible provider failure.

### Typed agent definitions

`AgentDefinition[Deps, Output]` sets the model, dependencies, output, instructions, extensions, and run policy. Definitions are immutable.

### Normalized runtime data

Runs return Ovid-owned messages, events, results, usage values, and identifiers. Application code does not need provider response types.

### Tools and capabilities

Add only the tools an agent needs. Capabilities can contribute instructions, tools, toolsets, model settings, and service requirements. Duplicate IDs are errors.

### Usage and policy

Apply request, token, and tool-call limits to one run or a complete nested workflow. Configure retries, timeouts, concurrency, and end behavior in one run policy.

### Relay and Todo

Relay gives application-owned agents a typed message channel. Todo provides optional phased work state with replaceable storage. Neither feature starts or changes an agent unless the application adds its capability.

### Storage and transports

Use the conversation store protocol with your own database. Optional adapters can expose registered agents over HTTP, SSE, stdio, or AG-UI.

---

## Pydantic AI and Harness compatibility

Every default Ovid agent runs on Pydantic AI. `ovid-native` does not create a different agent type. It contributes Ovid capabilities to a normal Ovid Core agent.

Use `pydantic_ai_capability()` to add a Pydantic AI or Pydantic AI Harness capability beside Ovid capabilities:

```python
from pydantic_ai_harness.planning import Planning

from ovid_core.adapters.pydantic_ai import pydantic_ai_capability
from ovid_native.search import SearchCapability


capabilities = (
    SearchCapability[AppDependencies](),
    pydantic_ai_capability(Planning()),
)
```

The default compiler passes the exact capability instance to Pydantic AI. Its instructions, tools, deferred loading, ordering, per-run state, and lifecycle hooks keep their upstream behavior. Deferred capabilities must have an explicit ID.

Passthrough tools remain Pydantic AI tools. They do not gain Ovid approval metadata, `BaseToolHook` hooks, Ovid result validation, service binding, or Ovid tool timeouts. Capabilities that change model selection, messages, events, or durable execution can also conflict with Ovid routing and normalized runtime contracts. Test those combinations through the public Ovid run and stream APIs.

Use Ovid-owned capability types when you need the complete Ovid contract. See [Tools and capabilities](docs/content/api/extensions.md#pydantic-ai-capability-passthrough) for the exact boundary.

---

## Optional server support

Ovid Core can expose agents, but it is not a full web framework. The application still owns:

- User authentication and authorization.
- Database setup and data retention.
- TLS and network policy.
- Dependency construction.
- Deployment and process control.
- Telemetry export.

Read [Embed and expose agents](docs/content/guides/embed-agents.md) before adding a transport.

---

## Application ownership

Ovid Core does not discover configuration files, load application secrets, choose every tool, or create global services. The application passes one final configuration and explicitly selects each capability.

For fast Rust-backed workspace tools, install [ovid-native](https://github.com/EzyGang/ovid-native). Ovid Core does not depend on it.

---

## Development

Clone the repository, then run:

```bash
uv sync
uv run task ruff
uv run task ty-lint
uv run task vulture
uv run task tests
uv run task docs-build
uv build
```

Run the documentation site locally with:

```bash
uv run task docs-run
```

Ovid Core targets 100% branch coverage for its Python integration layer.

---

## Contributing

1. Open an issue for a large change or a new public contract.
2. Create a branch from `main`.
3. Add tests for changed behavior.
4. Run the development checks.
5. Open a pull request with a clear reason for the change.

Keep provider runtime types inside adapters. Keep application policy in the application. See [AGENTS.md](AGENTS.md) for the full repository rules.

---

## License

Ovid Core is licensed under the [MIT License](LICENSE).
