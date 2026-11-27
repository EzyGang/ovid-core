import asyncio

from ovid_core.errors import (
    AgentRunError,
    AuthenticationError,
    ContextWindowError,
    CredentialError,
    DeferredToolAnswerError,
    ExtensionCollisionError,
    NothingToResumeError,
    OvidCoreError,
    ProviderError,
    ResumeLimitError,
    TransportError,
)
from ovid_core.runtime.identifiers import RunId
from ovid_core.server.models import ProviderErrorDetails, ServerErrorResponse


class _UnknownAgentError(TransportError):
    pass


class _AuthorizationDeniedError(TransportError):
    pass


class _UnknownCommandError(TransportError):
    pass


class _CommandExecutionError(TransportError):
    pass


class _ConversationBusyError(TransportError):
    def __init__(self, run_id: RunId) -> None:
        super().__init__('Conversation already has an active run')
        self.run_id = run_id


class _DuplicateRequestError(TransportError):
    """Refuse a client request ID that an earlier run of the conversation already used."""

    def __init__(self, run_id: RunId, *, resumable: bool) -> None:
        message = 'Client request ID was already used'
        super().__init__(f'{message} by a run that did not finish. Send a resume request.' if resumable else message)
        self.run_id = run_id


class _DeferredToolsUnavailableError(TransportError):
    pass


def _server_error_from_exception(error: BaseException) -> ServerErrorResponse:
    if isinstance(error, asyncio.CancelledError):
        return ServerErrorResponse(code='run_cancelled', message='Run cancelled')

    if isinstance(error, AuthenticationError):
        return ServerErrorResponse(code='authentication_error', message='Provider authentication failed')

    if isinstance(error, CredentialError):
        return ServerErrorResponse(code='credential_error', message='Provider credentials are unavailable')

    if isinstance(error, TimeoutError):
        return ServerErrorResponse(code='timeout', message='Request timed out')

    if isinstance(error, OvidCoreError):
        return _ovid_error_response(error)

    return ServerErrorResponse(code='internal_error', message='Internal server error')


def _ovid_error_response(error: OvidCoreError) -> ServerErrorResponse:
    if isinstance(error, _UnknownAgentError):
        return ServerErrorResponse(code='agent_not_found', message='Agent was not found')

    if isinstance(error, _UnknownCommandError):
        return ServerErrorResponse(code='command_not_found', message='Command was not found')

    if isinstance(error, _AuthorizationDeniedError):
        return ServerErrorResponse(code='forbidden', message='Request is not authorized')

    if isinstance(error, _ConversationBusyError):
        return ServerErrorResponse(code='conversation_busy', message=str(error), run_id=error.run_id)

    if isinstance(error, _DuplicateRequestError):
        return ServerErrorResponse(code='duplicate_request', message=str(error), run_id=error.run_id)

    if isinstance(error, _DeferredToolsUnavailableError):
        return ServerErrorResponse(
            code='deferred_tools_unavailable', message='Server cannot resume deferred tool calls'
        )

    if isinstance(error, _CommandExecutionError):
        return ServerErrorResponse(code='command_failed', message='Command execution failed')

    if isinstance(error, ContextWindowError):
        return ServerErrorResponse(code='context_window_exceeded', message=str(error))

    if isinstance(error, DeferredToolAnswerError):
        return ServerErrorResponse(code='invalid_tool_answers', message=str(error))

    if isinstance(error, NothingToResumeError):
        return ServerErrorResponse(code='nothing_to_resume', message=str(error))

    if isinstance(error, ResumeLimitError):
        return ServerErrorResponse(code='resume_limit_exceeded', message=str(error))

    if isinstance(error, ExtensionCollisionError):
        return ServerErrorResponse(code='extension_collision', message=str(error))

    if isinstance(error, ProviderError):
        return ServerErrorResponse(
            code='provider_error',
            message=str(error),
            provider=ProviderErrorDetails(
                failure_kind=error.failure_kind,
                status_code=error.status_code,
                provider_code=error.provider_code,
                retry_after_seconds=error.retry_after_seconds,
            ),
        )

    if isinstance(error, AgentRunError):
        return ServerErrorResponse(code='agent_run_failed', message=str(error))

    return ServerErrorResponse(code='server_failure', message='Server operation failed')
