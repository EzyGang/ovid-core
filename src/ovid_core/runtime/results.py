from typing import Self

from pydantic import Field, JsonValue, model_validator

from ovid_core.messages.models import AgentMessage
from ovid_core.models import BaseModel
from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import Usage


class ResultMetadataEntry(BaseModel):
    key: str = Field(min_length=1)
    value: JsonValue

    @model_validator(mode='after')
    def reject_secret_keys(self) -> Self:
        normalized_key = self.key.casefold().replace('-', '_')

        if any(secret_key in normalized_key for secret_key in _SECRET_METADATA_KEYS):
            raise ValueError('result metadata keys cannot identify secret values')

        return self


class RunResult[Output](BaseModel):
    output: Output
    messages: tuple[AgentMessage, ...]
    usage: Usage
    run_id: RunId
    conversation_id: ConversationId
    metadata: tuple[ResultMetadataEntry, ...] = ()

    @model_validator(mode='after')
    def validate_normalized_values(self) -> Self:
        for message in self.messages:
            if message.run_id is not None and message.run_id != self.run_id:
                raise ValueError('message run_id must match result run_id')
            if message.conversation_id is not None and message.conversation_id != self.conversation_id:
                raise ValueError('message conversation_id must match result conversation_id')

        request_usage = (
            message.request_usage
            for message in self.messages
            if message.role == 'response' and message.request_usage is not None
        )
        expected_usage = Usage.from_requests(request_usage, tool_calls=self.usage.tool_calls)

        if expected_usage != self.usage:
            raise ValueError('result usage must equal the aggregate normalized request usage')

        metadata_keys = tuple(item.key for item in self.metadata)

        if len(metadata_keys) != len(set(metadata_keys)):
            raise ValueError('result metadata keys must be unique')

        return self


_SECRET_METADATA_KEYS = (
    'api_key',
    'access_token',
    'refresh_token',
    'api_token',
    'authorization',
    'password',
    'credential',
)
