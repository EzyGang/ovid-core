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
    edit_mode=EditMode.HASHLINE,
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
| `edit` | Required | Apply the currently selected edit mode |

All built-in and custom modes use the wire name `edit`. Hashline and apply-patch advertise text grammars when the model supports them and always accept the JSON `{ "input": "..." }` form. The schema, grammar, description, and source rendering are captured for each model step.

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

The unguessable four-hex tag identifies one retained observation for this session and path. Each two-hex line hash is a compact locator; authorization also checks the retained full digest for every referenced line. The ledger retains no historical source snapshot, and evicted tags never become valid again.

Empty ranges request a bounded full-file presentation. Several non-overlapping ranges may be requested together. Directory reads use `WorkspaceDirectoryReadRequest` and support depth one or two. Reads reject URLs, archives, SSH paths, binary content, invalid UTF-8, absolute paths, root traversal, and descendant symlink traversal. Accepted `.` components and either path separator normalize to one `/`-separated ledger, result, event, and conflict identity.

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

Locators include inclusive ranges (`N:HH.=M:HH`), syntax blocks (`N:HH*`), gaps (`<N:HH` and `>N:HH`), block-end gaps (`>N:HH*`), and file boundaries (`<^` and `>$`). `CUT` captures source into an anonymous or named register; a later `PUT` can paste it across sections. `REM` deletes the section path and `MV` moves its final edited source to a non-existing destination. Both directives require a complete observation whose full normalized digest is still current.

Hashline parses and semantically preflights the complete request before writing. It rejects unseen, changed, shifted, missing, duplicated, overlapping, or ambiguous locators without relocation. A concurrent change outside every referenced line may coexist when the referenced line numbers and retained full digests still match. Remove and move additionally bind the preflight file identity, stage the exact source in its own directory, and never delete a path that was swapped after preflight. Hashline never creates a missing path; use `write`.

Successful Hashline output contains bounded fresh line locators. Those exact returned lines can authorize the next edit without rereading.

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

All existing-file changes require compatible evidence from the same workspace session and path. Changed or removed lines must have been rendered and must still match their retained full digest. Gap insertion requires an unchanged adjacent rendered line. Delete and move require complete source coverage. Exact current lines from `read`, native grep, AST grep, FFF grep, and FFF multi-grep share the same ledger and can authorize Hashline. Path-only glob and FFF find results, approximate FFF matches, truncated lines, and stale results never authorize edits.

Every successful mutation:

- uses the workspace write coordinator
- increments the workspace revision and affected file generation
- publishes one `WorkspaceChangeEvent` per changed path
- returns typed `WorkspaceFileChange` values
- renders bounded final source in `post_edit_sources`
- authorizes only those final lines actually returned


## Custom providers and plugins

`WorkspaceSessionBuilder` accepts Ovid-owned files, observations, search, AST, FFF, and stable-view provider protocols. A rootless session exposes only explicitly supplied operations and requires an observation store when files are selected; installing a plugin never activates one. Custom files providers return normalized lines together with exact BOM, line-ending, and terminal-newline metadata so observation validation reconstructs the bytes that were identified. Native search, AST, and FFF can run against a provider's absolute, read-only `WorkspaceView`. FFF retains one view for its index lifetime, returns its revision with content results, and rejects later calls when the provider revision changes. View-backed AST proposals revalidate the provider revision and current files, then commit through the files provider rather than writing the materialized view.

Plugins register provider, configurator, and capability factories through `PluginRegistrar`, then applications select their namespaced IDs explicitly. `activate_workspace_services()` consumes the selected `PluginServiceFactories`: a provider returns `workspace_builder_binding(builder, provider_id=...)`, configurators obtain that same unfrozen builder through `require_workspace_builder()`, and the adapter builds each session only after every selected configurator has run. It publishes validated workspace bindings in deterministic order and `ActivatedWorkspaceServices.close()` shuts owned sessions down in reverse order. Duplicate, empty, unknown, replacement, and incompatible selections fail. Custom edit modes use globally namespaced IDs, declare required workspace operations, and return their complete `BaseTool` schema, description, parser, executor, and approval metadata from `EditModeProvider.bind()`.

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

Closed sessions reject new operations. Observation tags are scoped to one session and cannot authorize another session, even when both sessions use the same root.
