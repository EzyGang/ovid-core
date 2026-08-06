from dataclasses import dataclass

from ovid_core.runtime.identifiers import ConversationId, RunId
from ovid_core.usage.models import Usage


@dataclass(frozen=True, slots=True)
class RunContext[Deps]:
    deps: Deps
    run_id: RunId
    conversation_id: ConversationId
    usage: Usage = Usage()
