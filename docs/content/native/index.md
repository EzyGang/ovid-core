# Ovid Native

`ovid-native` provides Rust-backed operations and Ovid tool integrations. Applications install the package and explicitly add the capabilities they need to an `AgentDefinition`. Installation never activates tools or changes an agent definition.

## Install a capability profile

Declare each capability profile the application uses:

```toml
[project]
dependencies = [
  "ovid-native[ast,fff,search]>=0.1.0,<0.2.0",
]
```

Use the aggregate profile when the application needs every shipped integration:

```toml
[project]
dependencies = [
  "ovid-native[all]>=0.1.0,<0.2.0",
]
```

All `ovid-native` wheels contain the complete supported native surface. Extras select Python-only dependencies required by a capability. AST, FFF, and search currently have no extra Python dependencies, so `[ast]`, `[fff]`, `[search]`, `[all]`, and the base package resolve to the same files. Declaring profiles records the application's dependency contract and includes future domain-specific dependencies.

Python installers do not retain the requested extra as runtime state. Code cannot reliably reject a base installation after dependency resolution. Agent access remains protected through explicit capability composition and Ovid tool approval.

## Bind one workspace to an agent

`ovid_native.__init__` stays empty. Import each value from its owning domain. Agent capabilities resolve named
workspace services rather than receiving independently rooted engines:

```python
from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_core.services import AgentServices
from ovid_native.ast import AstCapability
from ovid_native.fff import FffCapability
from ovid_native.search import SearchCapability
from ovid_native.workspace import NativeWorkspaceSession, workspace_binding


workspace = NativeWorkspaceSession(root=Path('/workspace/project'))

definition = AgentDefinition[AppDependencies, str](
    model=ModelRef(name='primary'),
    deps_type=AppDependencies,
    output_type=str,
    services=AgentServices((workspace_binding(workspace),)),
    capabilities=(SearchCapability(), AstCapability(), FffCapability(include_grep=False)),
)
```

The three capabilities share one opaque session identity, canonical root, cancellation domain, AST proposal revision,
and FFF lifecycle. The named binding defaults to `default`; pass the same `name` to `workspace_binding` and each
capability to use another binding. Close the session when the agent lifetime ends:

```python
await workspace.close()
```

Closing cancels supported active work, waits for native operations, stops the FFF provider, and rejects later calls.
Installation still changes no agent behavior.

## Use direct engines outside agent capabilities

`SearchEngine`, `AstEngine`, and `FffEngine` remain available for direct application calls. Each direct engine owns
its native workspace state. Do not use direct engines to construct agent capabilities.

`SearchCapability` contributes `glob` and `grep`; `FffCapability` contributes `find_files`, indexed `grep`, and
`multi_grep`, with optional native `glob`; `AstCapability` contributes `ast_grep`, `ast_edit_preview`, and
`ast_edit_apply`. Avoid enabling both search and FFF `grep` on the same agent because effective tool names must be
unique.

## Runtime compatibility

```python
from ovid_native.runtime import runtime_info

info = runtime_info()
print(info.api_version)
```

`api_version` protects the private Python and Rust boundary. `NativeWorkspaceSession`, `AstEngine`, `FffEngine`, and
`SearchEngine` reject a compiled extension whose API version does not match their Python wrapper. The package version
and declared `ovid-core` range protect public compatibility. Domain metadata remains available from
`ovid_native.ast.ast_grep_version` and `ovid_native.fff.fff_version`.

See [workspace search](search.md), [warm indexed FFF search](fff.md), and [AST search and rewrites](ast.md) for direct
API and agent-tool usage.
