# Write your own capability

You can add your own workspace tools to an agent. Your capability uses the same workspace session as the native capabilities. It shares the root, the native handle, the observations and the lifecycle.

## Public modules

Import only from these modules. A module or a name with a leading underscore is private. A private module can change in any release.

| Module | Contents |
| --- | --- |
| `ovid_native.workspace.capability` | `WorkspaceCapability`, the base class for a capability that uses a workspace session |
| `ovid_native.workspace.toolsets` | `WorkspaceResultTool`, `WorkspaceSourceTool`, `WorkspaceSourceToolset`, `WorkspaceStepToolset`, `BoundWorkspaceToolset` and `file_page_notices` |
| `ovid_native.execution` | `run_native`, `run_native_translated`, `NativeErrorTranslator` and `NativeCancellation` |
| `ovid_native.workspace.models` | The `WorkspaceSession` protocol and the provider protocols |
| `ovid_native.workspace.operations` | `WorkspaceOperation`, `WORKSPACE_SERVICE_KEY` and `workspace_ref` |
| `ovid_native.workspace.evidence` | Source presentation and observation metadata |
| `ovid_native.workspace.service` | `NativeWorkspaceSession` and `workspace_binding` |

## Write a capability

Subclass `WorkspaceCapability`. Implement two methods:

- `_required_operations` returns the workspace operations that your tools use. Agent construction fails when the session does not have one of them.
- `_contribute` receives the bound session and returns the tools and the instructions.

`session.root` is the canonical workspace root. It is `None` for a rootless session.

```python
from dataclasses import dataclass, field

from ovid_core.capabilities.base import CapabilityContributions
from ovid_core.models import BaseModel
from ovid_core.tools.models import ToolResult
from ovid_native.files.models import WorkspaceFileReadRequest
from ovid_native.workspace.capability import WorkspaceCapability
from ovid_native.workspace.models import WorkspaceSession
from ovid_native.workspace.operations import WorkspaceOperation
from ovid_native.workspace.toolsets import WorkspaceResultTool


class LineCountArgs(BaseModel):
    path: str


class LineCount(BaseModel):
    root: str
    lines: int


class LineCountTool(WorkspaceResultTool[None, WorkspaceSession, LineCountArgs, ToolResult]):
    id = 'line_count'
    description = 'Count the lines of a workspace file.'
    args_type = LineCountArgs
    result_type = ToolResult

    async def _content(self, arguments: LineCountArgs) -> BaseModel:
        read = await self._provider.files.read_file(WorkspaceFileReadRequest(path=arguments.path))
        return LineCount(root=str(self._provider.root), lines=read.total_lines)


@dataclass(frozen=True, slots=True, kw_only=True)
class LineCountCapability(WorkspaceCapability[None]):
    id: str = field(default='line_count', init=False)
    description: str = field(default='Workspace line counts', init=False)

    def _required_operations(self) -> frozenset[WorkspaceOperation]:
        return frozenset((WorkspaceOperation.FILES,))

    def _contribute(self, session: WorkspaceSession) -> CapabilityContributions[None]:
        return CapabilityContributions(tools=(LineCountTool(provider=session),))
```

Add `LineCountCapability()` to `AgentDefinition.capabilities`, next to the binding from `workspace_binding(workspace)`.

## Run blocking work

Do not run blocking work on the event loop. Call `run_native` to run it in a worker thread. Call `run_native_translated` to also change native errors into your own error types with a `NativeErrorTranslator`.

## Versioning

Before version 1.0, a minor release can change the public API. Pin the minor version in your dependencies.

A change to the `WorkspaceSession` protocol always increments `WORKSPACE_SERVICE_KEY.api_version`. A capability requires the version that it was built with. A session binding with a different version does not satisfy the capability, and agent construction fails with an "is not bound" error. If you write your own session, declare the current version and implement all protocol members.
