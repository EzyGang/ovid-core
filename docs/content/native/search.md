# Workspace search

`ovid_native.search` embeds ripgrep's Rust crates for bounded text search and uses the shared native workspace scanner for path discovery. It does not spawn an `rg` process.

## Install and activate

Declare the search profile in application metadata:

```toml
[project]
dependencies = [
  "ovid-native[search]>=0.1.0,<0.2.0",
]
```

Installation exposes the module. It does not add tools to an agent. Bind the search capability to an explicitly named
workspace service:

```python
from pathlib import Path

from ovid_core.agents import AgentDefinition
from ovid_core.routing.models import ModelRef
from ovid_core.services import AgentServices
from ovid_native.search import SearchCapability
from ovid_native.workspace.service import NativeWorkspaceSession, workspace_binding


workspace = NativeWorkspaceSession(root=Path('/workspace/project'))

definition = AgentDefinition[AppDependencies, str](
    model=ModelRef(name='primary'),
    deps_type=AppDependencies,
    output_type=str,
    services=AgentServices((workspace_binding(workspace),)),
    capabilities=(SearchCapability(),),
)
```

`SearchCapability` contributes one instruction block and two essential read tools:

| Tool | Default timeout | Purpose |
| --- | --- | --- |
| `glob` | 5 seconds | Find files and directories through exact paths, directories, or glob patterns |
| `grep` | 30 seconds | Search UTF-8 file content with literal, Rust regex, or PCRE2 matching |

`SearchCapability(workspace='default')` selects the named binding. Its constructor does not accept a root or provider.

Both tools use normal read approval metadata. Omitting `SearchCapability` contributes neither tool.

## Discover paths

```python
from pathlib import Path

from ovid_native.search import GlobRequest, SearchEngine


search = SearchEngine(root=Path('/workspace/project'))


result = await search.glob(
    GlobRequest(
        patterns=('src/**/*.py', 'tests'),
        file_type='file',
        order='modified_desc',
        limit=200,
    )
)

for match in result.matches:
    print(match.path, match.modified_at)
```

Each pattern may identify an exact file, directory, or glob. Overlapping selections are deduplicated. Path ordering sorts relative paths ascending. Modification ordering sorts newest entries first, then uses the relative path as a tie-break.

Directories end with `/` and have `file_type='directory'`. `completion='complete'` proves the scanner exhausted the selection. `file_limit_reached` and `deadline_reached` describe partial scans. `truncated=True` means another qualifying entry may exist.

## Search content

```python
from ovid_native.search import GrepRequest, SearchScanOptions


result = await search.grep(
    GrepRequest(
        pattern=r'class\s+\w+',
        scan=SearchScanOptions(paths=('src/**/*.py',)),
        mode='regex',
        file_limit=20,
        matches_per_file=20,
        context_before=1,
        context_after=1,
    )
)

for file in result.files:
    for match in file.matches:
        print(file.path, match.range.start.line, match.line_text)
```

Direct calls default to strict `mode='regex'`. The `grep` agent tool defaults to `mode='auto'`, which retries the complete pattern as literal text only when Rust regex and PCRE2 both reject it. Results set `interpreted_as_literal=True` after that fallback.

Pattern modes:

- `regex` tries Rust regex first, then PCRE2 for features such as lookaround and backreferences. Invalid patterns raise `SearchPatternError`.
- `literal` escapes the complete pattern and uses Rust regex.
- `auto` follows regex behavior and falls back to a complete literal pattern after both regex engines reject it.

Case-sensitive matching is the default. Set `multiline=True` to permit matches across line boundaries.

## Pagination and coverage

Grep pages by matching file. `file_offset` skips matching files, `file_limit` bounds returned files, and `matches_per_file` prevents one hot file from consuming the response. Continue with `next_file_offset` when present.

Each file reports:

- `total_matches` and `total_matches_exact`
- `matches_truncated`
- searched and total byte counts
- whether byte coverage is complete

Files above `max_file_bytes` use `large_file_mode='prefix'` by default. Prefix mode searches exactly the allowed prefix and sets `coverage.complete=False`. Skip mode does not search oversized files and increments `skipped_large_files`. Binary and non-UTF-8 files are skipped and counted separately.

`files_with_matches_exact=False`, incomplete completion, or partial file coverage means absence is unproven.

## Workspace policy

Search and AST operations share these rules:

- The application supplies an explicit workspace root.
- Returned paths are relative and use `/` separators.
- Absolute paths and parent traversal are rejected.
- Descendant directory symlinks are not followed.
- Explicit file symlinks must resolve inside the workspace.
- `.ignore`, `.gitignore`, and repository excludes are respected by default.
- Global Git ignores are disabled for deterministic application behavior.
- Hidden files, `.git`, and `node_modules` are excluded by default.
- Files, bytes, matches, context, line width, and time are bounded.

Set the corresponding request flags to include hidden files, ignore repository rules, or include `node_modules`.

## Engine ceilings

`SearchLimits` sets application-owned ceilings for scan entries, glob results, grep files, retained matches, matches per file, bytes per file, context lines, displayed line characters, and timeouts. Requests may choose lower values. A request above an engine ceiling raises `SearchLimitError` before native work begins.

Cancelling the awaiting task signals the native operation. Native traversal, reads, and match collection check the same cooperative cancellation state.
