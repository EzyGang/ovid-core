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


def should_fallback(error: Exception) -> bool:
    return provider_failure_kind(error) in {
        ProviderFailureKind.RATE_LIMIT,
        ProviderFailureKind.TIMEOUT,
        ProviderFailureKind.UNAVAILABLE,
    }
