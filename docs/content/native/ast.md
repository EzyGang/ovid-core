# AST search and rewrites

`ovid_native.ast` embeds ast-grep for syntax-aware search and staged structural rewrites. Patterns match parsed syntax, so source text inside comments and strings does not satisfy code patterns.

## Agent tools

`AstCapability` contributes three tools:

| Tool | Access | Purpose |
| --- | --- | --- |
| `ast_grep` | Read | Search syntax trees and return typed matches, captures, counts, and issues |
| `ast_edit_preview` | Read | Compute exact changes and store an expiring proposal without writing files |
| `ast_edit_apply` | Approval required | Apply one stored proposal after content-hash validation |

`ast_edit_apply` accepts only a proposal ID. Pattern, replacement, and path selection stay fixed after preview.

`AstCapability(workspace='default')` resolves the AST provider from that named `NativeWorkspaceSession` during agent
construction. It does not accept an independently rooted engine.

## Construct an engine

```python
from pathlib import Path

from ovid_native.ast import AstEngine, AstLimits


engine = AstEngine(
    root=Path('/workspace/project'),
    limits=AstLimits(
        max_matches=500,
        max_files=10_000,
        max_file_bytes=4 * 1024 * 1024,
        max_replacements=5_000,
        max_changed_files=1_000,
        proposal_ttl_seconds=600,
        max_pending_proposals=32,
    ),
)
```

The constructor resolves the root once and rejects a missing root or a file. Each engine owns its proposal store, proposal lock, and workspace write lock. Retain the engine for as long as its proposals should remain valid.

## Search syntax

```python
from ovid_native.ast import AstSearchRequest

result = await engine.search(
    AstSearchRequest(
        pattern='print($VALUE)',
        language='python',
    )
)

for match in result.matches:
    print(match.path, match.range.start.line, match.text)
```

Pattern metavariables follow ast-grep syntax:

- `$NAME` captures one node
- `$_` matches one node without binding it
- `$$$ARGS` captures zero or more nodes
- Reusing a metavariable requires identical syntax in each position

Set `language` to apply one language to every selected file. Omit it to infer the language from each file extension. Use `supported_ast_languages()` to inspect canonical identifiers, aliases, and extensions.

Strictness accepts `cst`, `smart`, `ast`, `relaxed`, `signature`, and `template`. The default is `smart`.

In Hashline mode, the agent-facing `ast_grep` tool validates the exact current source lines covering each match and capture, then renders shared Hashline locators. Those lines authorize direct edits; parse issues and path-only metadata do not.

## Select workspace files

```python
from ovid_native.ast import AstScanOptions

scan = AstScanOptions(
    paths=('src/**/*.py', 'tests'),
    include_hidden=False,
    respect_gitignore=True,
    include_node_modules=False,
)
```

Paths and globs are relative to the configured root. Scanning rejects absolute paths, parent traversal, and symlink targets outside the root. It skips `.git`, hidden files, and `node_modules` by default and never follows directory symlinks.

Candidate paths are deduplicated and sorted by root-relative POSIX path. Only regular UTF-8 files are parsed. Unsupported extensions and per-file read or parse failures appear in typed result fields.

## Preview and apply a rewrite

```python
from ovid_native.ast import (
    AstRewriteApplyRequest,
    AstRewriteOperation,
    AstRewritePreviewRequest,
    AstScanOptions,
)

preview = await engine.preview_rewrite(
    AstRewritePreviewRequest(
        operations=(
            AstRewriteOperation(
                pattern='print($VALUE)',
                replacement='logger.info($VALUE)',
            ),
        ),
        scan=AstScanOptions(paths=('src/**/*.py',)),
        language='python',
    )
)

for change in preview.changes:
    print(change.path, change.before, change.after)

applied = await engine.apply_rewrite(
    AstRewriteApplyRequest(proposal_id=preview.proposal_id)
)
```

Preview evaluates every operation against the same original syntax tree. It deduplicates identical edits and rejects divergent overlaps. A preview with no changes stores no proposal and returns an empty proposal ID.

Apply removes the proposal before writing, so each proposal can be used once. It preflights every affected file and rejects the complete operation when any SHA-256 hash changed. Each file is replaced through a same-directory temporary file, and its permissions are preserved. Filesystems do not provide a single atomic transaction across multiple paths.

Use `reject_rewrite(proposal_id)` to discard a proposal before expiration.

## Positions, issues, and errors

Lines and columns are one-based. Columns count Unicode characters. Byte offsets are zero-based UTF-8 offsets, and range ends are exclusive.

Search and preview return `AstIssue` values for parse errors, read errors, unsupported files, and reached limits. Request-wide failures use narrow exceptions:

- `AstConfigurationError`
- `AstPathError`
- `AstLanguageError`
- `AstPatternError`
- `AstLimitError`
- `AstProposalNotFoundError`
- `AstProposalExpiredError`
- `AstProposalStaleError`
- `AstWriteError`

Native filesystem and parsing work runs on worker threads. Cancelling the awaiting task signals cooperative cancellation between files and matches.
