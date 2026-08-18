# Ovid Native

`ovid-native` provides Rust-backed operations and Ovid tool integrations. Applications install the package and explicitly add the capabilities they need to an `AgentDefinition`. Installation never activates tools or changes an agent definition.

## Install a capability profile

Declare each capability profile the application uses:

```toml
[project]
dependencies = [
  "ovid-native[ast,fff,files,search]>=0.1.0,<0.2.0",
]
```

Use the aggregate profile when the application needs every shipped integration:

```toml
[project]
dependencies = [
  "ovid-native[all]>=0.1.0,<0.2.0",
]
```

Every `ovid-native` wheel contains the complete supported native surface.
Extras select the Python-only dependencies for a capability.
AST, FFF, files, and search currently have no extra Python dependencies.
Their named profiles, `[all]`, and the base package resolve to the same files.
Declaring a profile records the application dependency contract.
The profile can also include future domain dependencies.

Python installers do not retain the requested extra as runtime state.
Code cannot reliably reject a base installation after dependency resolution.
Explicit capability composition and Ovid tool approval protect agent access.

## Import from the owning module

Keep `ovid_native.__init__` and `ovid_native.workspace.__init__` empty.
Import each public value from its owning module:

```python
from ovid_native.ast import AstCapability, AstEngine
from ovid_native.fff import FffCapability, FffEngine
from ovid_native.files import WorkspaceFilesCapability
from ovid_native.search import SearchCapability, SearchEngine, SearchLimits
from ovid_native.workspace.builder import WorkspaceSessionBuilder
from ovid_native.workspace.service import NativeWorkspaceSession, workspace_binding
```

The direct engine classes remain available for application calls.
Agent capabilities resolve providers from one named workspace service.
They do not accept independently rooted engines.

## Add native tools to an agent

```python
from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_core.services import AgentServices
from ovid_native.ast import AstCapability
from ovid_native.fff import FffCapability
from ovid_native.search import SearchCapability
from ovid_native.workspace.service import NativeWorkspaceSession, workspace_binding


workspace = NativeWorkspaceSession(root=Path('/workspace/project'))

definition = AgentDefinition[AppDependencies, str](
    model=ModelRef(name='primary'),
    deps_type=AppDependencies,
    output_type=str,
    services=AgentServices((workspace_binding(workspace),)),
    capabilities=(
        SearchCapability(),
        AstCapability(),
        FffCapability(include_grep=False),
    ),
)
```

Each capability contributes a bounded set of tools:

- `WorkspaceFilesCapability` contributes `read`, `write`, and one selected edit tool.
- `SearchCapability` contributes `glob` and `grep`.
- `FffCapability` contributes `find_files`, indexed `grep`, and `multi_grep`.
- `FffCapability` can also contribute native `glob`.
- `AstCapability` contributes `ast_grep`, `ast_edit_preview`, and `ast_edit_apply`.

`AgentFactory` uses the existing capability adapter.
It needs no native-specific configuration.

All capabilities resolve the same root, native handle, session identity, revision domain, and lifecycle.
FFF disables its `grep` tool here because search already owns that wire name.
The `find_files` and `multi_grep` tools remain available.
Use distinct binding names only for deliberate multi-workspace agents.

Missing services or operations stop agent construction.
Call `await workspace.close()` when the agent lifetime ends.
Close is idempotent.
It stops the lazily started FFF provider before it closes the shared native handle.

## Build and override a workspace

`WorkspaceSessionBuilder.native(root=..., ast_limits=AstLimits(...))` creates native defaults.
The AST limits control search and proposal retention.
`with_native_ast(limits=...)` applies the same immutable limits to view-backed AST.
Provider selectors cover files, observations, search, AST, FFF, and stable local views.

You can select each slot once.
The builder validates required methods immediately.
A rootless workspace requires explicit providers.

Provider protocols use only Ovid request and result models.
`WorkspaceViewProvider.acquire_view()` supplies a contained, read-only local view.
The absolute view has one stable revision for its context lifetime.
Native search and AST use bounded view contexts.
FFF retains one view during indexing and closes it with the session.

View-backed AST proposals use the configured maximum count and monotonic TTL.
They revalidate the view revision and current files before commit.
The files provider performs the commit.

Plugins register namespaced service, capability, and custom edit-mode factories.
Installation alone changes no agent.
`ovid_native.workspace.plugins.activate_workspace_services()` applies selected configurators to an unfrozen `WorkspaceSessionBuilder`.
It then publishes the completed binding and owns reverse-order shutdown.
Applications can still construct `SearchEngine`, `AstEngine`, and `FffEngine` directly.

## Runtime compatibility

```python
from ovid_native.runtime import runtime_info

info = runtime_info()
print(info.api_version)
```

`api_version` protects the private Python and Rust boundary.
The engine and workspace classes reject an extension with a different API version.
The package version and declared `ovid-core` range protect public compatibility.
Domain metadata remains available from `ovid_native.ast.ast_grep_version` and `ovid_native.fff.fff_version`.

The following guides contain direct API and agent-tool examples:

- [safe workspace files](files.md)
- [workspace search](search.md)
- [warm indexed FFF search](fff.md)
- [AST search and rewrites](ast.md)
