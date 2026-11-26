from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ovid_core.capabilities.base import BaseCapability
from ovid_core.identifiers import ConversationId, RunId
from ovid_core.tools.deferred import DeferredToolAnswer, DeferredToolRequests


class DeferredToolAnswerer(Protocol):
    @abstractmethod
    async def __call__(
        self,
        requests: DeferredToolRequests,
        *,
        conversation_id: ConversationId,
        run_id: RunId,
    ) -> Mapping[str, DeferredToolAnswer]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class DeferredToolAnswerCapability[Deps](BaseCapability[Deps]):
    """Answer deferred tool calls during the run.

    The answerer returns answers for the calls that it can answer. The other calls stay in the `DeferredRunResult`.
    """

    answerer: DeferredToolAnswerer
