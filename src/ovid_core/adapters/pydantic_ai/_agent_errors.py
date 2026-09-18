from pydantic import ValidationError
from pydantic_ai.exceptions import UsageLimitExceeded

from ovid_core.adapters.pydantic_ai._provider_errors import provider_failure_kind
from ovid_core.errors import (
    AgentRunError,
    AgentTimeoutError,
    AuthenticationError,
    CredentialError,
    OvidCoreError,
    UsageLimitError,
)
from ovid_core.policy import ProviderFailureKind


def normalize_run_error(error: Exception) -> OvidCoreError:
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

    return AgentRunError('Agent run failed')
