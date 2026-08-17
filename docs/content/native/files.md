# Safe workspace files

`ovid_native.files` provides bounded UTF-8 reads, directory listings, explicit file creation, guarded whole-file replacement, and observation-authorized edits. Files run on the same `NativeWorkspaceSession` used by native search, FFF, and AST operations.

Installation does not activate file access. Applications must bind a workspace service and add `WorkspaceFilesCapability` explicitly.

## Activate the files capability

```python
from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_core.services import AgentServices
from ovid_native.files import EditMode, WorkspaceFilesCapability
from ovid_native.workspace.service import NativeWorkspaceSession, workspace_binding


workspace = NativeWorkspaceSession(
    root=Path('/workspace/project'),
    edit_mode=EditMode.APPLY_PATCH,
)

definition = AgentDefinition[AppDependencies, str](
    model=ModelRef(name='primary'),
    deps_type=AppDependencies,
    output_type=str,
    services=AgentServices((workspace_binding(workspace),)),
    capabilities=(WorkspaceFilesCapability(),),
)
```

The capability contributes:

| Tool | Approval | Purpose |
| --- | --- | --- |
| `read` | Not required | Read bounded text lines or list one workspace directory |
| `write` | Required | Create a file or replace a completely observed file |
| `edit` or `apply_patch` | Required | Apply the currently selected edit mode |

`replace` and `patch` modes use the wire name `edit`. `apply_patch` mode advertises `apply_patch` with a text grammar when the model supports it and always accepts the JSON `{ "input": "..." }` form. Every mode retains the stable Ovid tool ID `native_files_edit`.

## Read and observe source

```python
from ovid_native.files import ReadLineRange, WorkspaceFileReadRequest


result = await workspace.files.read_file(
    WorkspaceFileReadRequest(
        path='src/service.py',
        ranges=(ReadLineRange(start=40, end=60),),
    )
)
print(result.render())
```

Editable output uses one canonical representation:

```text
[src/service.py#A1B2]
40:7A|def load_user(user_id):
41:0D|    return repository.load(user_id)
```

The four-hex tag identifies the complete normalized file. Each two-hex line hash is display evidence only. Mutation authority comes from the session's compact observation ledger, which retains full content and line digests for exactly the source lines rendered by `read`.

Empty ranges request a bounded full-file presentation. Several non-overlapping ranges may be requested together. Directory reads use `WorkspaceDirectoryReadRequest` and support depth one or two. Reads reject URLs, archives, SSH paths, binary content, invalid UTF-8, absolute paths, root traversal, and descendant symlink traversal.

Large files remain bounded. A file above `max_observation_file_bytes` can be displayed only without an authorizing observation and is not editable.

## Create or replace a complete file

```python
from ovid_native.files import WorkspaceCreateRequest, WorkspaceReplaceRequest


created = await workspace.files.create_file(
    WorkspaceCreateRequest(path='src/generated.py', content='value = 1\n')
)

read = await workspace.files.read_file(
    WorkspaceFileReadRequest(path='src/generated.py')
)
assert read.observation is not None

replaced = await workspace.files.replace_file(
    WorkspaceReplaceRequest(
        path='src/generated.py',
        content='value = 2\n',
        expected_observation=read.observation.tag,
    )
)
```

Creation rejects an existing path. Parent creation requires both `create_parents=True` and `WorkspacePolicy.create_parent_directories=True`. Replacement requires a current, complete observation of the existing regular text file. File replacement uses a same-directory temporary file and an atomic per-file commit.

The model-facing `write` tool dispatches these operations through `WorkspaceWriteRequest.operation`.

## Select an edit mode

### Replace

```python
from ovid_native.files import ReplaceEditRequest


result = await workspace.files.replace(
    ReplaceEditRequest(
        path='src/service.py',
        old_string='return old_value',
        new_string='return new_value',
    )
)
```

Replace requires an exact unique match unless `replace_all=True`. Fuzzy recovery is disabled by default and, when enabled by policy, requires one candidate at or above the configured threshold. Every intersecting source line must have been rendered and must remain unchanged.

### Structured patch

```python
from ovid_native.files import PatchEditEntry, PatchEditRequest


result = await workspace.files.patch(
    PatchEditRequest(
        path='src/service.py',
        edits=(
            PatchEditEntry(
                operation='update',
                diff='@@\n-return old_value\n+return new_value',
            ),
        ),
    )
)
```

Structured patches support create, update, delete, and move operations. An update with `destination` performs a move. Every hunk needs a change and unique context; delete and move require a complete observation.

### Apply patch

```python
from ovid_native.files import ApplyPatchEditRequest


result = await workspace.files.apply_patch(
    ApplyPatchEditRequest(
        input='''*** Begin Patch
*** Update File: src/service.py
@@
-return old_value
+return new_value
*** Add File: src/generated.py
+value = 1
*** End Patch'''
    )
)
```

Apply-patch supports multi-file add, update, delete, and move envelopes. A patch is bounded to 4 MiB and 256 operations. The engine preflights every source and destination path, observation, and hunk before its first commit, then commits in authored order. A failure after one or more filesystem commits raises `WorkspacePartialCommitError` with landed and pending paths; it never claims multi-file atomicity.

## Change mode and policy live

```python
workspace.edit_mode.set(EditMode.REPLACE)
workspace.policy.update(
    allow_fuzzy_replace=True,
    fuzzy_replace_threshold=0.95,
)
```

The next model step receives the new edit schema and description without rebuilding the agent. An already advertised call retains its captured edit-mode and policy generations. Each result reports those generations.

`WorkspacePolicy` also bounds read bytes, observation file bytes, retained observation entries, and retained observation-store bytes. Policy and mode state belong to the shared workspace session, so every bound consumer sees the same current generation.

## Mutation safety and results

All existing-file changes require a compatible observation from the same workspace session and path. Changed or removed lines must have been rendered and must still match their retained full digest. Gap insertion requires an unchanged adjacent rendered line. Delete and move require complete source coverage. Search results do not authorize file edits.

Every successful mutation:

- uses the workspace write coordinator
- increments the workspace revision and affected file generation
- publishes one `WorkspaceChangeEvent` per changed path
- returns typed `WorkspaceFileChange` values
- renders bounded final source in `post_edit_sources`
- authorizes only those final lines actually returned

Close the owning workspace when the application is finished:

```python
await workspace.close()
```

Closed sessions reject new operations. Observation tags are scoped to one session and cannot authorize another session, even when both sessions use the same root.
