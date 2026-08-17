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

`ovid_native.__init__` stays empty. Import public values from the domain that owns them:

```python
from ovid_native.ast import AstCapability, AstEngine
from ovid_native.fff import FffCapability, FffEngine
from ovid_native.files import WorkspaceFilesCapability
from ovid_native.search import SearchCapability, SearchEngine, SearchLimits
```

Each domain module exports its requests, results, exceptions, tool classes, capability, engine, and public metadata types.

## Add native tools to an agent

```python
from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_native.search import SearchCapability, SearchEngine


engine = SearchEngine(root=Path('/workspace/project'))

definition = AgentDefinition[AppDependencies, str](
    model=ModelRef(name='primary'),
    deps_type=AppDependencies,
    output_type=str,
    capabilities=(SearchCapability(engine=engine),),
)
```

The application owns the workspace root and engine lifetime. `WorkspaceFilesCapability` contributes bounded `read` and guarded `write` tools plus one dynamically selected edit tool; `SearchCapability` contributes `glob` and `grep`; `FffCapability` contributes `find_files`, indexed `grep`, and `multi_grep`, with optional native `glob`; `AstCapability` contributes `ast_grep`, `ast_edit_preview`, and `ast_edit_apply`. `AgentFactory` uses the existing capability adapter and needs no native-specific configuration.

## Runtime compatibility

```python
from ovid_native.runtime import runtime_info

info = runtime_info()
print(info.api_version)
```

`api_version` protects the private Python and Rust boundary. `NativeWorkspaceSession`, `AstEngine`, `FffEngine`, and `SearchEngine` reject a compiled extension whose API version does not match their Python wrapper. The package version and its declared `ovid-core` range protect public compatibility. Domain metadata remains available from `ovid_native.ast.ast_grep_version` and `ovid_native.fff.fff_version`.

See [safe workspace files](files.md), [workspace search](search.md), [warm indexed FFF search](fff.md), and [AST search and rewrites](ast.md) for direct API and agent-tool usage.
