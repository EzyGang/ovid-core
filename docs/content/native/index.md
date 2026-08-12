# Ovid Native

`ovid-native` provides Rust-backed operations and Ovid tool integrations. Applications install the package and explicitly add the capabilities they need to an `AgentDefinition`. Installation never activates tools or changes an agent definition.

## Install a capability profile

Declare the profile in application dependency metadata:

```toml
[project]
dependencies = [
  "ovid-native[ast]>=0.1.0,<0.2.0",
]
```

Use the aggregate profile when the application needs every shipped integration:

```toml
[project]
dependencies = [
  "ovid-native[all]>=0.1.0,<0.2.0",
]
```

All `ovid-native` wheels contain the complete supported native surface. Extras select Python-only dependencies required by a capability. AST currently has no extra Python dependency, so `[ast]`, `[all]`, and the base package resolve to the same files. Declaring `[ast]` still records the application's dependency contract and will include future AST-specific dependencies.

Python installers do not retain the requested extra as runtime state. Code cannot reliably reject a base installation after dependency resolution. Agent access remains protected through explicit capability composition and Ovid tool approval.

## Import from the owning module

`ovid_native.__init__` stays empty. Import public values from the domain that owns them:

```python
from ovid_native.ast import AstCapability, AstEngine, AstLimits
```

The AST module also exports its requests, results, exceptions, tool classes, strictness type, issue type, language metadata, and embedded ast-grep version.

## Add native tools to an agent

```python
from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_native.ast import AstCapability, AstEngine


engine = AstEngine(root=Path('/workspace/project'))

definition = AgentDefinition[AppDependencies, str](
    model=ModelRef(name='primary'),
    deps_type=AppDependencies,
    output_type=str,
    capabilities=(AstCapability(engine=engine),),
)
```

The application owns the workspace root and engine lifetime. One `AstCapability` contributes one instruction block and the `ast_grep`, `ast_edit_preview`, and `ast_edit_apply` tools. `AgentFactory` uses the existing capability adapter; it needs no native-specific configuration.

## Runtime compatibility

```python
from ovid_native.runtime import runtime_info

info = runtime_info()
print(info.api_version)
print(info.ast_grep_version)
```

`api_version` protects the private Python and Rust boundary. The package version and its declared `ovid-core` range protect public compatibility.

See [AST search and rewrites](ast.md) for direct API and agent-tool usage.
