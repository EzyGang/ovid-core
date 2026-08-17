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

All `ovid-native` wheels contain the complete supported native surface. Extras select Python-only dependencies required by a capability. AST, FFF, files, and search currently have no extra Python dependencies, so `[ast]`, `[fff]`, `[files]`, `[search]`, `[all]`, and the base package resolve to the same files. Declaring profiles records the application's dependency contract and includes future domain-specific dependencies.

Python installers do not retain the requested extra as runtime state. Code cannot reliably reject a base installation after dependency resolution. Agent access remains protected through explicit capability composition and Ovid tool approval.

## Import from the owning module

`ovid_native.__init__` and `ovid_native.workspace.__init__` stay empty. Import public values from the module that owns
them:

```python
from ovid_native.ast import AstCapability, AstEngine
from ovid_native.fff import FffCapability, FffEngine
from ovid_native.files import WorkspaceFilesCapability
from ovid_native.search import SearchCapability, SearchEngine, SearchLimits
from ovid_native.workspace.builder import WorkspaceSessionBuilder
from ovid_native.workspace.service import NativeWorkspaceSession, workspace_binding
```

The direct engine classes remain available for application calls. Agent capabilities resolve providers from one named
workspace service instead of accepting independently rooted engines.

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

The application owns the workspace root and engine lifetime. `WorkspaceFilesCapability` contributes bounded `read` and guarded `write` tools plus one dynamically selected edit tool; `SearchCapability` contributes `glob` and `grep`; `FffCapability` contributes `find_files`, indexed `grep`, and `multi_grep`, with optional native `glob`; `AstCapability` contributes `ast_grep`, `ast_edit_preview`, and `ast_edit_apply`. `AgentFactory` uses the existing capability adapter and needs no native-specific configuration.

The capabilities resolve the same canonical root, native handle, session identity, revision domain, and lifecycle. FFF disables its `grep` tool here because search already owns that wire name; `find_files` and `multi_grep` remain available. Use distinct binding names only for deliberate multi-workspace agents. Missing services or operations fail during agent construction. Call `await workspace.close()` when the agent lifetime ends; close is idempotent and stops the lazily started FFF provider before closing the shared native handle.

## Build and override a workspace

`WorkspaceSessionBuilder.native(root=..., ast_limits=AstLimits(...))` creates native defaults with explicit AST search and proposal-retention limits. `with_native_ast(limits=...)` applies the same immutable limits to view-backed AST. Provider selectors cover files, observations, search, AST, FFF, and stable local views; every slot can be selected once and required methods are validated immediately. A rootless workspace requires explicit providers.

Provider protocols use Ovid request and result models only. `WorkspaceViewProvider.acquire_view()` supplies an absolute, contained, read-only local view with one stable revision for the context lifetime. Native search and AST use bounded view contexts. FFF retains one view through indexing and closes it with the session. View-backed AST proposals use the configured maximum count and monotonic TTL, revalidate the view revision and current files, then commit through the files provider.

Plugins register namespaced service, capability, and custom edit-mode factories explicitly. Installation alone changes no agent. `ovid_native.workspace.plugins.activate_workspace_services()` applies selected workspace configurators to an unfrozen `WorkspaceSessionBuilder`, then publishes the completed binding and owns reverse-order shutdown. Direct `SearchEngine`, `AstEngine`, and `FffEngine` construction remains supported for application calls.

## Runtime compatibility

```python
from ovid_native.runtime import runtime_info

info = runtime_info()
print(info.api_version)
```

`api_version` protects the private Python and Rust boundary. `NativeWorkspaceSession`, `AstEngine`, `FffEngine`, and `SearchEngine` reject a compiled extension whose API version does not match their Python wrapper. The package version and its declared `ovid-core` range protect public compatibility. Domain metadata remains available from `ovid_native.ast.ast_grep_version` and `ovid_native.fff.fff_version`.

See [safe workspace files](files.md), [workspace search](search.md), [warm indexed FFF search](fff.md), and [AST search and rewrites](ast.md) for direct API and agent-tool usage.
