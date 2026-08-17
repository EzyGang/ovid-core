# Warm indexed FFF search

`ovid_native.fff` provides typo-resistant path ranking and repeated content search through a long-lived `fff-search` index. Use it with native workspace search when an agent needs fuzzy discovery or repeated queries over the same repository.

## Install and activate

Declare the FFF profile in application metadata:

```toml
[project]
dependencies = [
  "ovid-native[fff]>=0.1.0,<0.2.0",
]
```

Installation exposes the module. It does not add tools to an agent. FFF initializes lazily from the shared workspace
when a selected tool first needs the index:

```python
from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_core.services import AgentServices
from ovid_native.fff import FffCapability
from ovid_native.workspace.service import NativeWorkspaceSession, workspace_binding


workspace = NativeWorkspaceSession(root=Path('/workspace/project'))

definition = AgentDefinition[AppDependencies, str](
    model=ModelRef(name='primary'),
    deps_type=AppDependencies,
    output_type=str,
    services=AgentServices((workspace_binding(workspace),)),
    capabilities=(FffCapability(),),
)
```

`FffCapability` contributes deferred essential-read tools:

| Tool | Default timeout | Purpose |
| --- | --- | --- |
| `find_files` | 10 seconds | Rank indexed files or directories by approximate path |
| `grep` | 10 seconds | Search indexed content with plain, regex, fuzzy, or automatic matching |
| `multi_grep` | 10 seconds | Search several literal naming variants with OR semantics |

Omitting `FffCapability` contributes no FFF tools. Call `await workspace.close()` when the agent lifetime ends.
Applications using the direct provider API can construct and own an engine separately:

```python
from ovid_native.fff import FffEngine


fff = FffEngine(root=Path('/workspace/project'))
await fff.wait_ready()
```

## Find approximate paths

```python
from ovid_native.fff import FffConstraints, FffFindRequest


result = await fff.find(
    FffFindRequest(
        query='credentail resolver',
        constraints=FffConstraints(include=('src/',), exclude=('tests/',)),
        kind='file',
        limit=20,
    )
)

for match in result.matches:
    print(match.path, match.exact_match, match.git_status)
```

Use one or two short query terms. Multiple terms narrow one ranked search. Set `kind` to `file`, `directory`, or `any`. Directory paths end with `/`. Continue from `next_offset` when present.

Result order carries FFF ranking. `exact_match` identifies an exact path match without exposing unstable scoring internals.

## Search indexed content

```python
from ovid_native.fff import FffGrepRequest


result = await fff.grep(
    FffGrepRequest(
        query=r'class\s+CredentialResolver',
        mode='regex',
        limit=20,
        matches_per_file=10,
        context_before=1,
        context_after=1,
    )
)

for match in result.matches:
    print(match.path, match.line_number, match.line)
```

Modes:

- `plain` performs literal text matching.
- `regex` validates the expression before native search. Invalid expressions raise `FffPatternError`.
- `fuzzy` performs approximate content matching and marks results as approximate.
- `auto` selects plain or regex from the query, then retries with fuzzy matching when the selected mode produces no page.

`actual_mode`, `fallback_from`, and `approximate` report how the query ran. Matches use one-based line and column values. `match_ranges` contains zero-based byte ranges within the matched line.

In Hashline mode, agent-facing FFF content tools pass exact indexed lines and the stable view revision through the shared observation service before rendering editable locators. A stale revision or changed current line yields no misleading tag. Fuzzy and auto-fallback approximate matches are explicitly uneditable. `find_files` remains path-only.

## Search naming variants together

```python
from ovid_native.fff import FffMultiGrepRequest


result = await fff.multi_grep(
    FffMultiGrepRequest(
        patterns=('CredentialResolver', 'credential_resolver', 'credentialResolver'),
        limit=20,
    )
)
```

`multi_grep` searches all patterns in one indexed operation. Patterns are literals, including regex punctuation. The result shape and file-offset pagination match `grep`.

## Pagination and indexed coverage

FFF content results page by searched file. `file_offset` selects the starting file and `limit` bounds the page. Continue from `next_file_offset` when present. `matches_per_file` bounds hot files.

`completion` has four values:

- `complete` means the current indexed and searchable universe was exhausted.
- `page_limit_reached` means another file page may exist.
- `time_budget_reached` means the configured search budget ended before completion.
- `index_incomplete` means initial indexing had not completed.

`indexed_files` counts indexed paths. `searchable_files` counts files eligible for content search after binary, size, and index filtering. An empty FFF result does not prove workspace-wide absence. Use native `glob` or `grep` when exact scan coverage matters.

## Configure lifecycle and limits

```python
from ovid_native.fff import FffConfig, FffEngine, FffLimits


fff = FffEngine(
    root=Path('/workspace/project'),
    config=FffConfig(
        watch=True,
        enable_content_indexing=True,
        enable_mmap_cache=False,
        initial_scan_timeout_seconds=30.0,
        search_timeout_seconds=5.0,
    ),
    limits=FffLimits(
        max_results=200,
        max_matches_per_file=100,
        max_file_bytes=10 * 1024 * 1024,
    ),
)
```

The engine starts lazily on `start()`, `wait_ready()`, or the first operation that requires startup. Concurrent startup calls share one picker. `wait_ready()` runs off the event loop and waits up to the initial scan timeout. `rescan()` requests a new full scan. With `watch=True`, the picker applies later filesystem events to the same index.

Requests may choose values at or below `FffLimits`. A request above an engine ceiling raises `FffLimitError` before search. Cancelling an awaiting grep signals the native abort state. Cancelling a readiness wait leaves the shared index running.

FFF uses the canonical workspace root, relative model-facing paths, repository ignore rules, and no symlink traversal outside the root. It excludes ignored, binary, and oversized content from the searchable universe.

## Keep exact glob with FFF

FFF does not implement exact glob discovery. Request the shared workspace search provider when the same capability should
also contribute `glob`:

```python
from ovid_native.fff import FffCapability


capability = FffCapability(include_glob=True)
```

The resulting tool set is `glob`, `find_files`, `grep`, and `multi_grep`. Avoid adding a separate `SearchCapability`
with FFF `grep` enabled because both capabilities use the `grep` wire name. To keep native grep, use
`FffCapability(include_grep=False)` alongside `SearchCapability()`; both resolve the same named workspace.

## Select a startup fallback

Choose the backend before constructing the agent when FFF startup should fall back to native search:

```python
from ovid_native.fff import select_fff_search_backend


capability = await select_fff_search_backend(workspace=workspace)
```

Successful startup returns `FffCapability` with native `glob`. An FFF startup failure or readiness timeout returns
`SearchCapability`. The selection remains fixed for the agent lifetime, while the workspace retains lifecycle ownership.
Search failures after successful selection remain visible and do not switch the tool schema.