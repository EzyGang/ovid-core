from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.models import ModelRequestContext

from ovid_core.adapters.pydantic_ai.messages import message_from_pydantic, message_to_pydantic
from ovid_core.capabilities.history import HistoryProcessorCapability


class HistoryProcessorAdapter[Deps](AbstractCapability[Deps]):
    def __init__(self, source: HistoryProcessorCapability[Deps]) -> None:
        self.id = source.id
        self.description = source.description
        self.defer_loading = source.defer_loading
        self._processor = source.processor

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position='innermost')

    async def before_model_request(
        self,
        ctx: RunContext[Deps],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        history = tuple(message_from_pydantic(message) for message in request_context.messages)
        processed = await self._processor(history)
        messages = [message_to_pydantic(message) for message in processed]
        request_context.messages = messages.copy()
        ctx.messages[:] = messages

        return request_context
