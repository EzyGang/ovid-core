from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from ovid_core.policy import ProviderFailureKind


def provider_failure_kind(error: BaseException) -> ProviderFailureKind:
    if isinstance(error, TimeoutError):
        return ProviderFailureKind.TIMEOUT
    if isinstance(error, ModelHTTPError):
        if error.status_code in (401, 403):
            return ProviderFailureKind.AUTHENTICATION
        if error.status_code == 429:
            return ProviderFailureKind.RATE_LIMIT
        if error.status_code in (408, 504):
            return ProviderFailureKind.TIMEOUT
        if error.status_code >= 500:
            return ProviderFailureKind.UNAVAILABLE

        return ProviderFailureKind.INVALID_REQUEST
    if isinstance(error, ModelAPIError):
        return ProviderFailureKind.UNAVAILABLE

    return ProviderFailureKind.UNKNOWN


def is_context_window_error(error: BaseException) -> bool:
    if not isinstance(error, ModelHTTPError) or error.status_code not in (400, 413):
        return False

    pending = [error.body]
    values: list[str] = []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.get(key) for key in ('error', 'code', 'type', 'message') if key in value)
        elif isinstance(value, str):
            values.append(value.casefold())
    detail = ' '.join(values)

    return any(
        marker in detail
        for marker in (
            'context_length_exceeded',
            'context_window_exceeded',
            'maximum context length',
            'prompt is too long',
            'too many tokens',
            'input token count',
        )
    )


def should_fallback(error: Exception) -> bool:
    return provider_failure_kind(error) in {
        ProviderFailureKind.RATE_LIMIT,
        ProviderFailureKind.TIMEOUT,
        ProviderFailureKind.UNAVAILABLE,
    }
