# Errors

Import the general core error hierarchy from `ovid_core.errors`, Relay-specific errors from `ovid_core.relay`, and Todo-specific errors from `ovid_core.todo.errors`.

Errors contain source-safe messages. Adapters remove provider response bodies, credentials, headers, signed URLs, and protocol values when necessary.

Ovid Core does not convert cancellation to a normal error.

```text
OvidCoreError
├── ConfigurationError
├── CredentialError
│   └── AuthenticationError
│       └── CodexAuthError
├── ProviderError
├── PersistenceError
│   └── TodoStoreError
├── ModelResolutionError
├── AgentConstructionError
│   └── ExtensionCollisionError
├── AgentRunError
│   ├── AgentTimeoutError (also TimeoutError)
│   ├── UsageLimitError
│   └── AgentRunInputError
│       ├── DeferredToolAnswerError
│       ├── NothingToResumeError
│       └── ResumeLimitError
├── ToolError
│   ├── ToolValidationError
│   └── ToolExecutionError
│       ├── ToolRetryError
│       └── ToolTimeoutError (also TimeoutError)
├── ToolApprovalRequired
├── ToolDeferred
├── PluginError
├── TodoError
│   ├── TodoOperationError
│   │   ├── TodoTargetNotFoundError
│   │   ├── TodoDuplicateError
│   │   └── TodoLimitError
│   └── TodoConflictError
├── RelayError
│   ├── UnknownRelayRecipientError
│   ├── RelayCapacityError
│   ├── RelayUnavailableError
│   └── RelayAddressInUseError
└── TransportError
    └── ServerConstructionError
```

| Exception | Raised for |
| --- | --- |
| `OvidCoreError` | Common base for catch-all Ovid Core failures. |
| `ConfigurationError` | Unsupported schema versions and invalid migration behavior. |
| `CredentialError` | Missing, unsupported, or failed credential resolution. |
| `AuthenticationError` | Unknown providers, invalid authentication flows, inactive sessions, and failed generic provider authentication. |
| `CodexAuthError` | Codex browser login, device login, token parsing, refresh, and credential-storage failures. |
| `ProviderError` | Provider request failures after retries, and invalid or unsupported provider messages, usage, and results. Server code `provider_error`. |
| `PersistenceError` | Invalid or unsupported persisted message records. |
| `ModelResolutionError` | Unknown selectors, alias collisions, provider construction, Codex catalog, or incompatible model settings. |
| `AgentConstructionError` | Agent or adapter extension compilation failures, and invalid Agent Skills libraries at run start. |
| `ExtensionCollisionError` | Duplicate capability, tool, or toolset IDs, and a tool name that two toolsets share. Server code `extension_collision`. |
| `AgentRunError` | Normalized agent execution failures. |
| `AgentTimeoutError` | Whole-run timeout. Catch this error as `AgentRunError` or built-in `TimeoutError`. |
| `UsageLimitError` | Preflight or post-update aggregate usage limit violation. |
| `AgentRunInputError` | A run with neither a prompt nor resume input, or a prompt together with `InterruptedRunResume`. |
| `DeferredToolAnswerError` | Resume answers that miss a pending call, name a call that is not pending, or do not match the call kind. |
| `NothingToResumeError` | An interrupted-run resume of a history without an interrupted run, or of a conversation with pending deferred calls. Server code `nothing_to_resume`. |
| `ResumeLimitError` | A server resume after `resume_restart_limit` or more stopped runs in a row. Server code `resume_limit_exceeded`. |
| `ToolError` | Base for Ovid tool failures. |
| `ToolValidationError` | Tool argument validation failure when `AgentRetryPolicy.tool_failures` is `'raise'`. |
| `ToolExecutionError` | Tool implementation or hook execution failure. A tool raises this error to report a failure that the model cannot correct with a new call. |
| `ToolRetryError` | A tool raises this error to tell the model to correct the call. It uses the tool retry budget. |
| `ToolTimeoutError` | Tool timeout. Catch this error as `ToolExecutionError` or built-in `TimeoutError`. |
| `ToolApprovalRequired` | A tool raises this signal to pause the run until the application approves the call. It is not a failure. |
| `ToolDeferred` | A tool raises this signal to give the call to the application, which supplies the result. It is not a failure. |
| `PluginError` | Plugin-boundary failure for consumers implementing plugin support. |
| `TodoError` | Base for Todo reduction and optimistic-concurrency failures. |
| `TodoOperationError` | Invalid operation for the current state or target status. |
| `TodoTargetNotFoundError` | Exact task or phase target is absent. |
| `TodoDuplicateError` | Duplicate phase name or globally duplicate task content. |
| `TodoLimitError` | Loaded or proposed state exceeds configured limits. |
| `TodoConflictError` | Store revision changed before a save or explicitly revisioned operation. |
| `TodoStoreError` | Source-safe custom Todo backend failure. |
| `RelayError` | Base for Relay connection and mailbox failures. |
| `UnknownRelayRecipientError` | Relay send to an address unavailable on the connection. |
| `RelayCapacityError` | Relay mailbox capacity rejection; accepted messages are never silently dropped. |
| `RelayUnavailableError` | Operation attempted through a closed or otherwise unavailable Relay connection. |
| `RelayAddressInUseError` | Duplicate live address registration in a Relay network. |
| `TransportError` | Normalized transport and server-runtime failure. |
| `ServerConstructionError` | Missing optional dependencies, incompatible agent runtime, or invalid server registration. |

The server reports two request conflicts with error codes and no public exception class:

- `conversation_busy`: the conversation has an active run. The error contains the `run_id` of that run.
- `duplicate_request`: an earlier run of the conversation used the same `client_request_id`. The error contains the `run_id` of that run. [Client request IDs](server.md#client-request-ids) describes the check.

## Tool errors and the agent run

By default, a tool error does not come out of `OvidAgent.run` or `OvidAgent.stream`.
The adapter returns the failure to the model as a `failed` or `retried` tool result.
Refer to [Tool failure results](extensions.md#tool-failure-results) for the full rules.

A run raises `AgentRunError` when a tool has no retry budget left.
A run raises a tool error only for a host fault or when `AgentRetryPolicy.tool_failures` is `'raise'`.

## Provider errors

A run raises `ProviderError` when a provider request fails after the retries of `AgentRunPolicy.provider_retry`.
Authentication failures, unavailable credentials, timeouts, and context-window overflows keep their own error types.
`ProviderError` has these attributes:

| Attribute | Default | Meaning |
| --- | --- | --- |
| `failure_kind` | `ProviderFailureKind.UNKNOWN` | Classification of the failure. |
| `status_code` | `None` | Provider HTTP status. |
| `provider_code` | `None` | Error code that the provider sent. |
| `retry_after_seconds` | `None` | Wait in seconds that the provider asked for. |

The message states the failure kind, the status, and the requested wait.
It never contains the provider error message.
When a route has more than one candidate and all candidates fail, the run raises `AgentRunError`.

## Catching errors

Catch the narrowest useful boundary:

```python
from ovid_core.errors import ModelResolutionError

try:
    agent = await factory.build(definition)
except ModelResolutionError as error:
    report_configuration_problem(str(error))
```

Do not catch `BaseException`. In async code, allow `asyncio.CancelledError` to propagate.
