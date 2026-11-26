from collections.abc import Awaitable, Callable, Sequence

from pydantic_ai import RunContext as PydanticRunContext

from ovid_core.adapters.pydantic_ai._tool_context import run_context_from_pydantic
from ovid_core.capabilities.base import InstructionSource
from ovid_core.runtime.context import RunContext


type PydanticInstruction[Deps] = str | Callable[[PydanticRunContext[Deps]], Awaitable[str | None]]


def pydantic_instructions[Deps](sources: Sequence[InstructionSource[Deps]]) -> list[PydanticInstruction[Deps]]:
    """Keep static text and wrap each dynamic source, so Pydantic AI evaluates it on every model request."""
    return [source if isinstance(source, str) else _instruction_function(source) for source in sources]


def _instruction_function[Deps](
    source: Callable[[RunContext[Deps]], str | None | Awaitable[str | None]],
) -> Callable[[PydanticRunContext[Deps]], Awaitable[str | None]]:
    async def instruction(ctx: PydanticRunContext[Deps]) -> str | None:
        result = source(run_context_from_pydantic(ctx))
        if isinstance(result, Awaitable):
            return await result

        return result

    return instruction
