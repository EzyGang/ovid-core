# Workspace commands and jobs

`ovid_native.command` runs shell commands from the workspace root. A foreground command returns one bounded result. A command job runs in the background, and the application reads its output while the job runs.

The command domain is trusted host execution. A command can read and change paths outside the workspace. It is not a sandbox. The application owns the model-facing tools, approval policy, and result presentation.

## Run a foreground command

`NativeWorkspaceSession.command` runs a command through an embedded Bash-compatible shell. The command reads from a null input.

```python
from pathlib import Path

from ovid_native.command.models import WorkspaceCommandRequest
from ovid_native.workspace.service import NativeWorkspaceSession


async def build(root: Path) -> None:
    session = NativeWorkspaceSession(root=root)
    try:
        result = await session.command.execute(
            WorkspaceCommandRequest(command='cargo build', timeout_seconds=600, max_head_bytes=4_096)
        )
        print(result.exit_code, result.tail)
    finally:
        await session.close()
```

A request has these limits:

- `cwd` must resolve to a directory in the workspace.
- `timeout_seconds` is between 1 and 3600 seconds.
- `max_output_bytes` limits the output that the result keeps.

The result keeps the first `max_head_bytes` as `head` and the last bytes as `tail`. `omitted_bytes` counts the bytes between them. A cut never splits a UTF-8 character.

Each command owns a process scope. The scope is a Job Object on Windows and one process group on Unix. When the command completes, times out, or is cancelled, Ovid Native stops every process in the scope.

## Start a command job

`NativeWorkspaceSession.command_jobs` starts and controls command jobs. The `WorkspaceOperation.COMMAND_JOBS` operation enables it. Native sessions enable the operation by default.

```python
from ovid_native.command.models import WorkspaceCommandJobRequest

jobs = session.command_jobs
started = await jobs.start(WorkspaceCommandJobRequest(command='python -m http.server 8000'))
```

`start` returns a `WorkspaceCommandJobSnapshot`. Use its `job_id` in each later call. A job has these request fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `command` | required | The command text |
| `cwd` | workspace root | A directory in the workspace |
| `env` | empty | Variables that replace or add to the host environment |
| `timeout_seconds` | `None` | Stops the job after 1 to 86400 seconds. `None` sets no limit |
| `pty` | `False` | Runs the job on a pseudoterminal |
| `cols`, `rows` | 120, 40 | The terminal size for a PTY job |
| `max_buffer_bytes` | 1 MiB | The output that the job keeps, from 64 KiB to 8 MiB |

A job without a PTY reads its input from a pipe. Send input with `write`. The job keeps its process scope after its command ends. Processes that the command starts in the background continue to run until the job stops.

## Read job output

`poll(job_id, offset=0, strip_ansi=False)` returns the output from `offset` to the end. Pass the `next_offset` of the previous snapshot to get only new output.

```python
snapshot = await jobs.poll(started.job_id)
offset = snapshot.next_offset
snapshot = await jobs.poll(started.job_id, offset=offset)
```

The job merges standard output and standard error. Offsets count every byte that the job printed, so they only grow. The job keeps the first 64 KiB as the head and a rolling tail. When the buffer is full, the job drops the oldest tail bytes.

A snapshot has these output fields:

- `head` holds the requested bytes that are in the first 64 KiB.
- `tail` holds the requested bytes that the rolling tail still keeps.
- `omitted_bytes` counts the requested bytes that the job dropped.
- `truncated_before` counts every byte that the job dropped.
- `next_offset` is the offset for the next poll.

While the job prints, a poll stops before an incomplete UTF-8 character. The next poll returns the whole character. `strip_ansi=True` removes ANSI escape sequences from the returned text. The job keeps the original bytes.

## Control a job

| Method | Result |
| --- | --- |
| `write(job_id, data)` | Sends text to the job input |
| `resize(job_id, cols=..., rows=...)` | Changes the terminal size of a PTY job |
| `signal(job_id, 'interrupt')` | Sends Ctrl+C to a PTY job |
| `signal(job_id, 'terminate')` | Stops every process of the job after a grace period of 500 ms on Unix |
| `signal(job_id, 'kill')` | Stops every process of the job at once |
| `wait(job_id, timeout_seconds=...)` | Waits for the job to stop and returns a snapshot |
| `list()` | Returns a snapshot of each job without output |

`wait` returns when the job stops and its output closes. Background processes can keep the output open, so `wait` returns at most 250 ms after the job stops. It also returns when `timeout_seconds` ends.

The snapshot `status` is one of these values:

- `running`: the command still runs.
- `exited`: the command ended with `exit_code`.
- `failed`: the shell could not run the command. The output holds the reason.
- `timed_out`: the job reached `timeout_seconds`.
- `killed`: a `terminate` or `kill` signal stopped the job.

## Run a job on a pseudoterminal

Set `pty=True` when a program must see a terminal. Examples are interactive prompts and programs that change their output for a terminal.

```python
started = await jobs.start(WorkspaceCommandJobRequest(command='python -i', pty=True, cols=100, rows=30))
await jobs.write(started.job_id, 'print(6 * 7)\r')
await jobs.resize(started.job_id, cols=132, rows=43)
```

A PTY job does not use the embedded shell. It runs the command through the platform shell:

- On Windows, `cmd.exe /d /s /c` runs the command on a pseudoconsole.
- On Unix, `/bin/sh -c` runs the command in a new session. The pseudoterminal is the controlling terminal of that session.

The shell syntax of a PTY job therefore differs from a foreground command and from a job without a PTY. Write the command for `cmd.exe` on Windows and for a POSIX shell on Unix. The `cwd` and `env` fields work the same for every job.

A terminal sends a carriage return for the Enter key. Send `\r` to answer a prompt. The terminal output contains escape sequences and echoes the input. Use `strip_ansi=True` to remove the escape sequences.

## Limits and cleanup

- A session runs at most 8 jobs at the same time. A ninth `start` raises `CommandJobLimitError`. A job that stopped does not count.
- All jobs of a session keep at most 8 MiB of output together. When the total is full, a job drops more of its tail. A job never drops its head.
- A stopped job stays available for 10 minutes. After that, the session removes the job and stops its remaining processes.
- `session.close()` stops every job and every process of each job.
- An unknown `job_id` raises `CommandJobNotFoundError`.

Ovid Native does not do these tasks:

- Jobs do not survive a restart of the host process.
- Ovid Native does not add model-facing command or job tools. The application owns them.
- Ovid Native does not notify the model when a job prints or stops. The application polls.
