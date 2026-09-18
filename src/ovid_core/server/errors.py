import asyncio

from ovid_core.errors import AgentRunError, AuthenticationError, CredentialError, OvidCoreError, TransportError
from ovid_core.server.models import ServerErrorResponse


class _UnknownAgentError(TransportError):
    pass


class _AuthorizationDeniedError(TransportError):
    pass


class _UnknownCommandError(TransportError):
    pass


class _CommandExecutionError(TransportError):
    pass


def _server_error_from_exception(error: BaseException) -> ServerErrorResponse:
    if isinstance(error, asyncio.CancelledError):
        return ServerErrorResponse(code='run_cancelled', message='Run cancelled')

    if isinstance(error, AuthenticationError):
        return ServerErrorResponse(code='authentication_error', message='Provider authentication failed')

    if isinstance(error, CredentialError):
        return ServerErrorResponse(code='credential_error', message='Provider credentials are unavailable')

    if isinstance(error, _UnknownAgentError):
        return ServerErrorResponse(code='agent_not_found', message='Agent was not found')

    if isinstance(error, _UnknownCommandError):
        return ServerErrorResponse(code='command_not_found', message='Command was not found')

    if isinstance(error, _AuthorizationDeniedError):
        return ServerErrorResponse(code='forbidden', message='Request is not authorized')

    if isinstance(error, TimeoutError):
        return ServerErrorResponse(code='timeout', message='Request timed out')

    if isinstance(error, _CommandExecutionError):
        return ServerErrorResponse(code='command_failed', message='Command execution failed')

    if isinstance(error, AgentRunError):
        return ServerErrorResponse(code='agent_run_failed', message=str(error))

    if isinstance(error, OvidCoreError):
        return ServerErrorResponse(code='server_failure', message='Server operation failed')

    return ServerErrorResponse(code='internal_error', message='Internal server error')
