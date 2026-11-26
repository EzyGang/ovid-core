from pydantic import ValidationError
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UsageLimitExceeded

from ovid_core.adapters.pydantic_ai._provider_errors import is_context_window_error, provider_failure_kind
from ovid_core.errors import (
    AgentRunError,
    AgentTimeoutError,
    AuthenticationError,
    ContextWindowError,
    CredentialError,
    OvidCoreError,
    ProviderError,
    UsageLimitError,
)
from ovid_core.policy import ProviderFailureKind


def normalize_run_error(error: Exception) -> OvidCoreError:
    input_limit_exceeded = isinstance(error, UsageLimitExceeded) and 'per_request_input_tokens_limit' in str(error)
    if input_limit_exceeded or is_context_window_error(error):
        return ContextWindowError('Model context window is full. Compact the conversation or start a new one')
    if isinstance(error, AuthenticationError):
        return AuthenticationError('Provider authentication failed')
    if isinstance(error, CredentialError):
        return CredentialError('Provider credentials are unavailable')
    if isinstance(error, OvidCoreError):
        return error
    if provider_failure_kind(error) is ProviderFailureKind.AUTHENTICATION:
        return AuthenticationError('Provider authentication failed')
    if isinstance(error, TimeoutError):
        return AgentTimeoutError('Agent run timed out')
    if isinstance(error, UsageLimitExceeded):
        return UsageLimitError('Agent usage limit exceeded')
    if isinstance(error, ValidationError):
        return AgentRunError('Pydantic AI returned invalid agent data')
    if not isinstance(error, ModelAPIError):
        return AgentRunError('Agent run failed')

    kind = provider_failure_kind(error)
    status_code = error.status_code if isinstance(error, ModelHTTPError) else None
    message = f'Provider request failed ({kind})'
    if status_code is not None:
        message = f'Provider request failed ({kind}, status {status_code})'
    if error.retry_after is not None:
        message = f'{message}. The provider asked for a wait of {error.retry_after:g} seconds before a retry'

    return ProviderError(
        message,
        failure_kind=kind,
        status_code=status_code,
        provider_code=error.provider_error_code,
        retry_after_seconds=error.retry_after,
    )
