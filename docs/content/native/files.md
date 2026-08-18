# Safe workspace files

`ovid_native.files` provides bounded UTF-8 reads, directory listings, file creation, guarded replacement, and observation-authorized edits.
Files use the same `NativeWorkspaceSession` as native search, FFF, and AST operations.

Installation does not activate file access.
Applications must bind a workspace service and add `WorkspaceFilesCapability` explicitly.

## Activate the files capability

```python
from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_core.services import AgentServices
from ovid_core.tools import ToolApproval
from ovid_native.files import EditMode, WorkspaceFilesCapability
from ovid_native.workspace.service import NativeWorkspaceSession, workspace_binding


workspace = NativeWorkspaceSession(
    root=Path('/workspace/project'),
    edit_mode=EditMode.HASHLINE,
)

definition = AgentDefinition[AppDependencies, str](
    model=ModelRef(name='primary'),
    deps_type=AppDependencies,
    output_type=str,
    services=AgentServices((workspace_binding(workspace),)),
    capabilities=(WorkspaceFilesCapability(),),
    tool_approval=ToolApproval(required=False),
)
```

The capability contributes:

| Tool | Default approval | Purpose |
| --- | --- | --- |
| `read` | Not required | Read bounded text lines or list one workspace directory |
| `write` | Required | Create a file or replace a completely observed file |
| `edit` | Required | Apply the current edit mode |

Approval is an application tool-call policy.
The example overrides the tool defaults.
It removes the approval pause for all Ovid tools in this agent.
Omit `tool_approval` to keep the defaults in the table.
Path policy, observation checks, validation, timeouts, and cancellation always apply.

All built-in and custom modes use the wire name `edit`.
Hashline and apply-patch advertise text grammars when the model supports them.
They always accept the JSON `{ "input": "..." }` form.
Each model step captures the schema, grammar, description, and source rendering.

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

The unguessable four-hex tag identifies one retained observation for this session and path.
Each two-hex line hash is a compact locator.
Authorization also checks the retained full digest for every referenced line.
The ledger retains no historical source snapshot.
Evicted tags never become valid again.

Empty ranges request a bounded full-file presentation.
You can request several non-overlapping ranges together.
Directory reads use `WorkspaceDirectoryReadRequest` and support depth one or two.
Reads reject these inputs:

- URLs, archives, and SSH paths
- binary content and invalid UTF-8
- absolute paths and root traversal
- descendant symlink traversal

Accepted `.` components and both path separators use one normalized `/`-separated identity.
The ledger, result, event, and conflict use this identity.

Large files remain bounded.
A file above `max_observation_file_bytes` cannot produce an authorizing observation.
Therefore, the file cannot authorize an edit.

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

Creation rejects an existing path.
Parent creation requires `create_parents=True`.
It also requires `WorkspacePolicy.create_parent_directories=True`.
Replacement requires a current and complete observation of the regular text file.
File replacement uses a temporary file in the same directory.
It makes an atomic commit for that file.

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

Replace requires one exact match unless `replace_all=True`.
Policy disables fuzzy recovery by default.
When enabled, fuzzy recovery requires one candidate at or above the configured threshold.
The operation must have rendered every intersecting source line.
Each line must remain unchanged.

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

Structured patches support create, update, delete, and move operations.
An update with `destination` performs a move.
Every hunk needs one change and unique context.
Delete and move require a complete observation.

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

Apply-patch supports multi-file add, update, delete, and move envelopes.
A patch has a 4 MiB limit and a 256-operation limit.
The engine checks every path, observation, and hunk before the first commit.
It then commits in authored order.
A later failure raises `WorkspacePartialCommitError` with landed and pending paths.
Apply-patch does not claim multi-file atomicity.

### Hashline

Hashline edits exact source rendered by `read`, native grep, AST grep, FFF grep, or a successful previous edit:

```text
*** Begin Patch
[src/service.py#A1B2]
PUT 40:7A.=41:0D:
+def load_user(user_id):
+    return repository.fetch(user_id)
*** End Patch
```

Locators support these forms:

- inclusive ranges with `N:HH.=M:HH`
- syntax blocks with `N:HH*`
- gaps with `<N:HH` and `>N:HH`
- block-end gaps with `>N:HH*`
- file boundaries with `<^` and `>$`

`CUT` captures source in an anonymous or named register.
A later `PUT` can paste the source across sections.
`REM` deletes the section path.
`MV` moves its final edited source to a destination that does not exist.
Both directives require a complete observation with a current normalized digest.

Hashline parses and checks the complete request before writing.
It rejects unseen, changed, shifted, missing, duplicate, overlapping, or ambiguous locators.
It does not relocate a locator.
A concurrent change can coexist when it occurs outside every referenced line.
The referenced line numbers and retained full digests must still match.

Remove and move also bind the file identity during the check.
They stage the exact source in its directory.
They never delete a path that another actor swapped after the check.
Hashline never creates a missing path.
Use `write` to create a file.

Successful Hashline output contains bounded fresh line locators.
These returned lines can authorize the next edit without another read.

## Change mode and policy live

```python
workspace.edit_mode.set(EditMode.REPLACE)
workspace.policy.update(
    allow_fuzzy_replace=True,
    fuzzy_replace_threshold=0.95,
)
```

The next model step receives the new edit schema and description.
The application does not need to rebuild the agent.
An advertised call retains its captured edit-mode and policy generations.
Each result reports those generations.

`WorkspacePolicy` also bounds read bytes and observation file bytes.
It bounds retained observation entries and retained store bytes.
Policy and mode state belong to the shared workspace session.
Therefore, every bound consumer sees the same current generation.

## Mutation safety and results

All existing-file changes require compatible evidence from the same workspace session and path.
The operation must have rendered every changed or removed line.
Each line must still match its retained full digest.
Gap insertion requires an unchanged adjacent rendered line.

Delete and move require complete source coverage.
Exact current lines from all workspace content tools share the same ledger.
These lines can authorize Hashline.
The following values cannot authorize edits:

- path-only glob and FFF find results
- approximate FFF matches
- truncated lines
- stale results

Every successful mutation:

- uses the workspace write coordinator
- increments the workspace revision and affected file generation
- publishes one `WorkspaceChangeEvent` per changed path
- returns typed `WorkspaceFileChange` values
- renders bounded final source in `post_edit_sources`
- authorizes only those final lines actually returned


## Custom providers and plugins

`WorkspaceSessionBuilder` accepts Ovid-owned provider protocols for each workspace operation.
A rootless session exposes only the supplied operations.
It requires an observation store when the application selects files.
Plugin installation does not activate a provider.
Custom files providers return normalized lines and exact file-format metadata.
The metadata covers the BOM, line endings, and terminal newline.

Observation validation uses this metadata to reconstruct the identified bytes.
Native search, AST, and FFF can use a provider's absolute read-only `WorkspaceView`.
FFF retains one view for the index lifetime.
Content results include its revision.

FFF rejects later calls after the provider revision changes.
View-backed AST proposals revalidate the revision and current files.
They commit through the files provider and do not write the materialized view.

Plugins register provider, configurator, and capability factories through `PluginRegistrar`.
Applications then select the namespaced IDs.
`activate_workspace_services()` consumes the selected `PluginServiceFactories`.
A provider returns `workspace_builder_binding(builder, provider_id=...)`.
Configurators obtain that unfrozen builder through `require_workspace_builder()`.
The adapter builds each session after all selected configurators run.

It publishes validated workspace bindings in deterministic order.
`ActivatedWorkspaceServices.close()` closes owned sessions in reverse order.
Invalid selections fail before activation.
This includes duplicate, empty, unknown, replacement, and incompatible selections.

Custom edit modes use globally namespaced IDs.
They declare required workspace operations.
`EditModeProvider.bind()` returns the complete tool definition and behavior.

```python
selected = registrar.select_service_factories(
    providers=('example.workspace',),
    configurators=('example.workspace.configure',),
)
activated = await activate_workspace_services(
    selected,
    context=PluginActivationContext(services=base_services),
    configs={'example.workspace': {'root': '/workspace/project'}},
)
workspace = activated.services.resolve(workspace_ref())
```

Close the owning workspace when the application is finished:

```python
await workspace.close()
```

Closed sessions reject new operations.
Observation tags belong to one session.
They cannot authorize another session, even when both sessions use the same root.
