from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ovid_core.capabilities.base import BaseCapability
from ovid_core.messages.models import AgentMessage


type MessageHistoryProcessor = Callable[
    [tuple[AgentMessage, ...]],
    Awaitable[tuple[AgentMessage, ...]],
]


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryProcessorCapability[Deps](BaseCapability[Deps]):
    processor: MessageHistoryProcessor
