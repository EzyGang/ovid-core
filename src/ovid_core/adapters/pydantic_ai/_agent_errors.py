from pydantic import ValidationError
from pydantic_ai.exceptions import UsageLimitExceeded

from ovid_core.errors import AgentRunError, AgentTimeoutError, OvidCoreError, UsageLimitError


def normalize_run_error(error: Exception) -> OvidCoreError:
    if isinstance(error, OvidCoreError):
        return error
    if isinstance(error, TimeoutError):
        return AgentTimeoutError('Agent run timed out')
    if isinstance(error, UsageLimitExceeded):
        return UsageLimitError('Agent usage limit exceeded')
    if isinstance(error, ValidationError):
        return AgentRunError('Pydantic AI returned invalid agent data')

    return AgentRunError('Agent run failed')
