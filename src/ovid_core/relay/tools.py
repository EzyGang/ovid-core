from pydantic import Field, JsonValue

from ovid_core.models import BaseModel
from ovid_core.relay.contracts import RelayConnection
from ovid_core.relay.models import RelayAddress, RelayContact, RelayMessage, RelayMessageId, RelayReceipt
from ovid_core.tools.base import BaseTool, ToolExecutionContext
from ovid_core.tools.models import ToolResult


_SEND_DESCRIPTION = (
    'Send a message to one Relay contact. Set reply_to to the ID of the message being answered; '
    'the recipient application may receive it automatically through its delivery handler.'
)
_WAIT_DESCRIPTION = (
    'Wait for and consume one Relay message. Filter by sender and/or exact reply_to message ID; '
    'automatic application delivery may consume messages before this tool sees them.'
)
_PENDING_DESCRIPTION = 'Return pending Relay messages in FIFO order; they are consumed unless retain is true.'
_CONTACTS_DESCRIPTION = (
    'List contacts on this Relay connection, excluding the bound identity and without presence status.'
)


class RelayToolDescriptions(BaseModel):
    send: str = Field(default=_SEND_DESCRIPTION, min_length=1)
    wait: str = Field(default=_WAIT_DESCRIPTION, min_length=1)
    pending: str = Field(default=_PENDING_DESCRIPTION, min_length=1)
    contacts: str = Field(default=_CONTACTS_DESCRIPTION, min_length=1)


class RelaySendArguments(BaseModel):
    to: RelayAddress
    message: str
    reply_to: RelayMessageId | None = None


class RelayWaitArguments(BaseModel):
    sender: RelayAddress | None = None
    reply_to: RelayMessageId | None = None
    timeout_seconds: float | None = Field(default=None, ge=0)


class RelayPendingArguments(BaseModel):
    retain: bool = False


class RelayContactsArguments(BaseModel):
    pass


class RelaySendResult(ToolResult):
    receipt: RelayReceipt


class RelayWaitResult(ToolResult):
    message: RelayMessage | None


class RelayPendingResult(ToolResult):
    messages: tuple[RelayMessage, ...]


class RelayContactsResult(ToolResult):
    contacts: tuple[RelayContact, ...]


class RelaySendTool[Deps](BaseTool[Deps, RelaySendArguments, RelaySendResult]):
    id = 'relay_send'
    description = _SEND_DESCRIPTION
    args_type = RelaySendArguments
    result_type = RelaySendResult

    def __init__(self, connection: RelayConnection, *, description: str = _SEND_DESCRIPTION) -> None:
        self._connection = connection
        self.description = description

    async def execute(
        self,
        context: ToolExecutionContext[Deps],
        arguments: RelaySendArguments,
    ) -> RelaySendResult:
        del context
        receipt = await self._connection.send(
            recipient=arguments.to,
            content=arguments.message,
            reply_to=arguments.reply_to,
        )

        return RelaySendResult(content=receipt.model_dump(mode='json'), receipt=receipt)


class RelayWaitTool[Deps](BaseTool[Deps, RelayWaitArguments, RelayWaitResult]):
    id = 'relay_wait'
    description = _WAIT_DESCRIPTION
    args_type = RelayWaitArguments
    result_type = RelayWaitResult

    def __init__(self, connection: RelayConnection, *, description: str = _WAIT_DESCRIPTION) -> None:
        self._connection = connection
        self.description = description

    async def execute(
        self,
        context: ToolExecutionContext[Deps],
        arguments: RelayWaitArguments,
    ) -> RelayWaitResult:
        del context
        message = await self._connection.wait(
            sender=arguments.sender,
            reply_to=arguments.reply_to,
            timeout_seconds=arguments.timeout_seconds,
        )
        content: JsonValue = None if message is None else message.model_dump(mode='json')

        return RelayWaitResult(content=content, message=message)


class RelayPendingTool[Deps](BaseTool[Deps, RelayPendingArguments, RelayPendingResult]):
    id = 'relay_pending'
    description = _PENDING_DESCRIPTION
    args_type = RelayPendingArguments
    result_type = RelayPendingResult

    def __init__(self, connection: RelayConnection, *, description: str = _PENDING_DESCRIPTION) -> None:
        self._connection = connection
        self.description = description

    async def execute(
        self,
        context: ToolExecutionContext[Deps],
        arguments: RelayPendingArguments,
    ) -> RelayPendingResult:
        del context
        messages = await self._connection.pending(retain=arguments.retain)
        content: JsonValue = [message.model_dump(mode='json') for message in messages]

        return RelayPendingResult(content=content, messages=messages)


class RelayContactsTool[Deps](BaseTool[Deps, RelayContactsArguments, RelayContactsResult]):
    id = 'relay_contacts'
    description = _CONTACTS_DESCRIPTION
    args_type = RelayContactsArguments
    result_type = RelayContactsResult

    def __init__(self, connection: RelayConnection, *, description: str = _CONTACTS_DESCRIPTION) -> None:
        self._connection = connection
        self.description = description

    async def execute(
        self,
        context: ToolExecutionContext[Deps],
        arguments: RelayContactsArguments,
    ) -> RelayContactsResult:
        del context, arguments
        contacts = await self._connection.contacts()
        content: JsonValue = [contact.model_dump(mode='json') for contact in contacts]

        return RelayContactsResult(content=content, contacts=contacts)
