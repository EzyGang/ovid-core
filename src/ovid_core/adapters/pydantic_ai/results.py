from pydantic import ValidationError
from pydantic_ai.run import AgentRunResult

from ovid_core.adapters.pydantic_ai.messages import message_from_pydantic
from ovid_core.adapters.pydantic_ai.usage import usage_from_pydantic
from ovid_core.errors import ProviderError
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.runtime.results import ResultMetadataEntry, RunResult


def result_from_pydantic[Output](value: AgentRunResult[Output]) -> RunResult[Output]:
    try:
        messages = tuple(message_from_pydantic(message) for message in value.new_messages())
        request_usage = tuple(
            message.request_usage
            for message in messages
            if message.role == 'response' and message.request_usage is not None
        )

        return RunResult[Output](
            output=value.output,
            messages=messages,
            usage=usage_from_pydantic(value.usage, request_usage),
            run_id=RunId(value.run_id),
            conversation_id=ConversationId(value.conversation_id),
            metadata=tuple(
                ResultMetadataEntry(key=key, value=item) for key, item in sorted((value.metadata or {}).items())
            ),
        )
    except (TypeError, ValidationError, ValueError) as error:
        raise ProviderError('Pydantic AI returned an invalid run result') from error
