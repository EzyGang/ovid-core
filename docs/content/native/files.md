# Safe workspace files

`ovid_native.files` provides bounded UTF-8 reads, directory listings, file creation, guarded replacement, and observation-authorized edits.
Files use the same `NativeWorkspaceSession` as native search, FFF, and AST operations.

Installation does not activate file access.
Applications must bind a workspace service and add `WorkspaceFilesCapability` explicitly.
`WorkspaceFilesCapability(defer_loading=True)` hides the file tools until the model calls `load_capability` with the ID `native_files`.

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

| Tool | Default approval | Effect | Purpose |
| --- | --- | --- | --- |
| `read` | Not required | `read`, `safe` | Read bounded text lines or list one workspace directory |
| `write` | Required | `workspace_write`, `unsafe` | Create a file or replace a completely observed file |
| `edit` | Required | `workspace_write`, `unsafe` | Apply the current edit mode |

The Effect column shows the `ToolEffect` of each tool.
The four built-in `edit` tools (`native_files_replace`, `native_files_patch`, `native_files_apply_patch`, and `native_files_hashline`) all declare `workspace_write` and `unsafe`.
[Declaring tool effects](../api/extensions.md#declaring-tool-effects) explains the values.
The `edit` tool comes from the dynamic toolset `native_files_edit_mode`.
Its `AgentToolsetDescriptor.approval` declares the same approval as the `edit` tools, so an application can show it before the run starts.

Approval is an application tool-call policy.
The example overrides the tool defaults.
It removes the approval pause for all Ovid tools in this agent.
Omit `tool_approval` to keep the defaults in the table.
A `write` or `edit` call then pauses the run, and `OvidAgent.run` returns a `DeferredRunResult`.
The application approves or denies each call in a resume run, as [Deferred runs](../api/agents.md#deferred-runs) shows.
An approved call runs once, with `ToolExecutionContext.approved=True`.
Path policy, observation checks, validation, timeouts, and cancellation always apply.

All built-in and custom modes use the wire name `edit`.
Hashline and apply-patch take their patch as text input.
They always accept the JSON `{ "input": "..." }` form.
The `edit` tool description states the Hashline or patch syntax.
No grammar goes to the model.
The Hashline grammar is kept only in `ToolDefinition.metadata['ovid_grammar']`, and no provider adapter reads it.
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

The model-facing `read` tool returns expected path, selector, and workspace read errors as diagnostic text in a successful result.
Each requested target has its own diagnostic text, thus one bad target does not hide the other targets.
A read of several targets ends with `[N of M targets failed]` when at least one target failed.
A file that is too large to edit has the notice `[Not editable: N bytes exceeds the LIMIT-byte edit limit]` after its lines.
The tool result `metadata` still holds the full structured result of each target, for user interfaces.
Its `observations` list holds the receipt of each editable file that the result rendered.
Ovid Core never sends `metadata` to the model.
By default, a different failure of a native tool does not stop the run.
Ovid Core returns the failure to the model as a `failed` tool result that contains the exception type and text.
Refer to [Tool failure results](../api/extensions.md#tool-failure-results) for the full rules.
Direct workspace provider calls still raise typed exceptions, and cancellation still propagates.

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
A parse error gives the line, the column, the expected tokens, and the first 120 characters of that input line.

Remove and move also bind the file identity during the check.
They stage the exact source in its directory.
They never delete a path that another actor swapped after the check.
Hashline never creates a missing path.
Use `write` to create a file.

Successful Hashline output contains bounded fresh line locators.
These returned lines can authorize the next edit without another read.

The output of every edit mode has these sections, in this order:

1. The rendered source of each edited file that the result can show.
2. One line that lists each change without rendered source, as `OPERATION: PATH` or `OPERATION: PATH -> DESTINATION`, separated by commas.
3. `[Fuzzy match applied with confidence 0.97. Check the edited lines]` when a fuzzy replace applied the edit, with the confidence to two decimals.
4. `[Commit incomplete: only the changes shown were written. Reread the files before you retry]` when the commit did not finish.

The tool result `metadata` still holds the full structured edit result, for user interfaces.
This includes each change, the generations, the matching strategy, and the source presentation.
The `observations` list holds the receipt of each rendered post-edit source.
Ovid Core never sends `metadata` to the model.

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

## Recover interrupted edits

A Hashline or patch edit can change several files.
A lost process can stop the edit after some files changed.
Give the session a state directory to make these edits recoverable.

```python
workspace = NativeWorkspaceSession(
    root=Path('/workspace/project'),
    state_dir=Path('/home/user/.ovid/workspaces/<root-digest>'),
)
reports = await workspace.recover()
```

The first journalled edit creates the state directory with `blobs/` and `mutations/`.
Without `state_dir`, the session does not journal edits, and `recover()` returns an empty tuple.
Command process records use `processes/` in the same state directory. See [Reap orphaned processes](command.md#reap-orphaned-processes).

Before the first file changes, each Hashline and patch edit does these steps:

1. It writes the bytes of each changed file to `blobs/<sha256>`. Edits share a blob when the bytes are equal.
2. It writes one record to `mutations/<id>.record`. For each file, the record keeps the path, operation, destination, and the normalized digests before and after the edit.
3. It syncs each new blob and the record to disk.

After each file changes, the edit appends one progress line to the record without a sync.
Recovery uses the file digests, not the progress lines.
When the edit ends, it deletes the record and each blob that no other record names.
An edit that fails in a live process also deletes its record.
`WorkspacePartialCommitError` then reports the changed and pending paths.

Call `recover()` once when the workspace opens, before the first edit.
It takes the exclusive workspace write lock, thus no live edit can run.
For each file of each leftover record, recovery applies the first rule that matches:

- A Hashline remove or move left the original in a `.ovid-hashline-*` folder, and the path is empty: recovery moves the original back.
- The file has the digest from before the edit: the edit did not change the file, and recovery does nothing.
- The file has the digest from after the edit: recovery writes the pre-image back. For a created file, recovery deletes the file.
- The file has other content: this is a conflict, and recovery does not change the file.

Then recovery deletes the record, its blobs, and the `.ovid-hashline-*` folders that the record names.
It does not touch other `.ovid-hashline-*` folders.
No record stays after a conflict, thus a later open does not report the same conflict again.

`recover()` returns one `WorkspaceRecoveryReport` for each leftover record:

- `mutation_id`: the ID of the record
- `restored`: the paths that recovery returned to their pre-images
- `conflicts`: the paths that changed after the crash and that recovery did not change

The report contains paths only.
It never contains file content.

The journal has these limits:

- Recovery never rolls back an external change. The user must resolve each conflict.
- The journal covers Hashline and patch edits only. Complete-file writes, replace edits, and AST rewrites are not journalled.
- Shell commands are not journalled.
- Each edit writes at most one blob for each changed file. It syncs each new blob and the record once.

## Restore observations after a restart

The observation ledger is in memory.
A new workspace session starts with an empty ledger.
Thus, after a restart, an old `[path#TAG]` anchor fails with "observation was not retained".
Read, `grep`, FFF `grep`, `multi_grep`, `ast_grep`, and edit results keep their receipts in the tool result `metadata`.
The key is `observations`, and it holds a list of objects with these fields:

- `path`: the normalized workspace path
- `tag`: the four-hex tag
- `content_sha256`: the full SHA-256 digest of the normalized file
- `visible_ranges`: the inclusive line ranges that the result rendered
- `complete_presentation`: `True` when the receipt covered the full file

Source tools write this key in the Hashline and the plain presentations.

```python
from ovid_native.files.rehydration import receipts_from_tool_results

requests = receipts_from_tool_results(part.metadata for part in tool_returns)
report = await workspace.observations.rehydrate(requests)
```

Give the metadata values in transcript order.
`receipts_from_tool_results` reads plain JSON and ignores values without a valid `observations` list.
For a result that was stored before tool metadata moved out of the content, the caller gives the `metadata` object from the stored envelope.

`rehydrate` processes the newest requests first.
For each request, it reads the file under the normal path and read policy.
It records the receipt only when the current digest is equal to `content_sha256`.
It authorizes only the lines in `visible_ranges`.
A complete presentation stays complete only when these lines cover the full file.

`WorkspaceRehydrationReport` has three counts:

- `restored`: requests that the ledger now authorizes
- `stale`: requests for a changed, missing, or unreadable file, or for a line that the file no longer has
- `skipped`: requests that the ledger cannot keep without an eviction

Processing stops at the first skipped request, and all older requests are also skipped.
A stale request records nothing, thus an edit with its anchor still fails.
Hashline named registers stay in process memory, and `rehydrate` does not restore them.

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
