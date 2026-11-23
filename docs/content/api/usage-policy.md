# Usage, policy, and observability

## Request and aggregate usage

Import from `ovid_core.usage.models`.

`ProviderUsageDetails = dict[str, dict[str, NonNegativeInt]]`. The outer key is a provider namespace. Values remain non-negative integer counters.

### `RequestUsage`

All counters default to zero:

- `input_tokens`
- `output_tokens`
- `cache_read_tokens`
- `cache_write_tokens`
- `input_audio_tokens`
- `output_audio_tokens`
- `cache_audio_read_tokens`
- `provider_details`

The `total_tokens` property adds `input_tokens` and `output_tokens`. It does not add cache and audio counters again.

### `Usage`

Adds `request_count` and `tool_calls` to the same token and provider-detail fields. Public operations:

```text
Usage.from_requests(requests: Iterable[RequestUsage] = (), *, tool_calls: int = 0) -> Usage
usage.total_tokens -> int
usage.is_zero -> bool
usage + other -> Usage
usage.delta_since(previous) -> Usage
```

`from_requests` counts and aggregates every request. Addition and delta operations apply to scalar counters and nested provider details. Values are immutable.

## `UsageTracker`

Import from `ovid_core.usage.tracking`.

```text
UsageUpdateCallback = Callable[[UsageTracker, Usage], Awaitable[None]]

UsageTracker(
    *,
    limits: AgentUsageLimits | None = None,
    on_update: UsageUpdateCallback | None = None,
)
```

Public API:

| Member | Contract |
| --- | --- |
| `usage` | Local ledger for this tracker. |
| `aggregate_usage` | Root ledger for a child tracker. Otherwise, this is local usage. |
| `limits` | Root limits when this is a child. |
| `create_child(on_update=None)` | Creates an empty local ledger that forwards every delta exactly once to its parent. |
| `await add(delta)` | Atomically adds a non-zero delta, forwards it, enforces root aggregate limits, then calls the local callback. |
| `check_before_request()` | Rejects a request when an aggregate request or token counter has already reached its limit. |
| `check_before_tool_call()` | Rejects a tool call when the aggregate tool-call counter has reached its limit. |

Limit violations raise `UsageLimitError`.

Ovid Core cannot count a failed provider attempt when the provider supplies no usage. It includes every request that the provider reports.

`tool_calls` counts only the tool calls that complete successfully.
A call that returns a `failed` or `retried` tool result does not add to `tool_calls`.
The model request that follows the result adds to `request_count`.

## Runtime policy

Import from `ovid_core.policy`.

### `AgentRetryPolicy`

| Field | Default | Meaning |
| --- | --- | --- |
| `tools` | `2` | Retry budget of each tool. Refer to [Tool failure results](extensions.md#tool-failure-results). |
| `output` | `1` | Number of times the model can correct an invalid final output. |
| `tool_failures` | `'return'` | `'return'` sends each tool failure to the model as a tool result. `'raise'` stops the run with the Ovid tool error. |
| `error_max_chars` | `2000` | Positive maximum length of the failure text that the model receives. |

Provider SDK retries remain provider-owned. `tools` and `output` configure agent tool and output validation retries.

`AgentRetryPolicy(tools=0, output=0, tool_failures='raise')` stops the run on the first tool failure, unknown tool name, or invalid output.

### `AgentUsageLimits`

| Field | Default |
| --- | --- |
| `requests` | `50` |
| `tool_calls` | `None` |
| `input_tokens` | `None` |
| `output_tokens` | `None` |
| `total_tokens` | `None` |
| `per_request_input_tokens` | `None` |
| `count_tokens_before_request` | `False` |

Numeric limits are positive when present.

### `AgentRunPolicy`

| Field | Default | Meaning |
| --- | --- | --- |
| `retries` | `AgentRetryPolicy()` | Agent-loop retry counts. |
| `limits` | `AgentUsageLimits()` | Aggregate run and nested-run limits. |
| `timeout_seconds` | `None` | Positive whole-run timeout when set. |
| `tool_timeout_seconds` | `30.0` | Positive default tool timeout. `None` disables the timeout. |
| `max_concurrency` | `None` | Positive concurrent run limit when set. |
| `end_strategy` | `'graceful'` | `early`, `graceful`, or `exhaustive` tool completion strategy. |

### `ProviderFailureKind`

`StrEnum` values: `AUTHENTICATION`, `RATE_LIMIT`, `TIMEOUT`, `UNAVAILABLE`, `INVALID_REQUEST`, and `UNKNOWN`. The Pydantic AI adapter uses these classifications to decide whether a fallback candidate is eligible.

## Observability

Import `ObservabilityConfig` from `ovid_core.observability`.

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `False` | Enable Pydantic AI instrumentation for the agent. |
| `include_content` | `False` | Include prompt, completion, binary, and model request parameter content. |

When enabled, Ovid Core maps this value to Pydantic AI `InstrumentationSettings`. Exporters, resources, processors, Logfire, and other OpenTelemetry application setup remain outside core. Content remains excluded unless explicitly enabled.
