# Errors

Import the general core error hierarchy from `ovid_core.errors`. Import Relay-specific errors from `ovid_core.relay`.

Errors contain source-safe messages. Adapters remove provider response bodies, credentials, headers, signed URLs, and protocol values when necessary.

Ovid Core does not convert cancellation to a normal error.

```text
OvidCoreError
├── ConfigurationError
├── CredentialError
│   └── CodexAuthError
├── ProviderError
├── PersistenceError
├── ModelResolutionError
├── AgentConstructionError
│   └── ExtensionCollisionError
├── AgentRunError
│   ├── AgentTimeoutError (also TimeoutError)
│   └── UsageLimitError
├── ToolError
│   ├── ToolValidationError
│   └── ToolExecutionError
│       └── ToolTimeoutError (also TimeoutError)
├── PluginError
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
| `CodexAuthError` | Codex browser login, device login, token parsing, refresh, and credential-storage failures. |
| `ProviderError` | Invalid or unsupported provider messages, usage, and results. |
| `PersistenceError` | Invalid or unsupported persisted message records. |
| `ModelResolutionError` | Unknown selectors, alias collisions, provider construction, Codex catalog, or incompatible model settings. |
| `AgentConstructionError` | Agent or adapter extension compilation failures. |
| `ExtensionCollisionError` | Duplicate capability, tool, or toolset IDs. |
| `AgentRunError` | Normalized agent execution failures. |
| `AgentTimeoutError` | Whole-run timeout. Catch this error as `AgentRunError` or built-in `TimeoutError`. |
| `UsageLimitError` | Preflight or post-update aggregate usage limit violation. |
| `ToolError` | Base for Ovid tool failures. |
| `ToolValidationError` | Tool argument or result validation failure. |
| `ToolExecutionError` | Tool implementation or hook execution failure. |
| `ToolTimeoutError` | Tool timeout. Catch this error as `ToolExecutionError` or built-in `TimeoutError`. |
| `PluginError` | Plugin-boundary failure for consumers implementing plugin support. |
| `RelayError` | Base for Relay connection and mailbox failures. |
| `UnknownRelayRecipientError` | Relay send to an address unavailable on the connection. |
| `RelayCapacityError` | Relay mailbox capacity rejection; accepted messages are never silently dropped. |
| `RelayUnavailableError` | Operation attempted through a closed or otherwise unavailable Relay connection. |
| `RelayAddressInUseError` | Duplicate live address registration in a Relay network. |
| `TransportError` | Normalized transport and server-runtime failure. |
| `ServerConstructionError` | Missing optional dependencies, incompatible agent runtime, or invalid server registration. |

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
