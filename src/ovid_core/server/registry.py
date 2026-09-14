from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, overload

from ovid_core.server.contracts import AgentRegistration


class AgentRegistry(Sequence[AgentRegistration[Any, Any]]):
    def __init__(self, agents: Sequence[AgentRegistration[Any, Any]] = ()) -> None:
        self._agents: list[AgentRegistration[Any, Any]] = []
        self._by_id: dict[str, AgentRegistration[Any, Any]] = {}
        self.register(agents)

    def register(self, agents: Sequence[AgentRegistration[Any, Any]]) -> None:
        """Register a batch atomically, rejecting duplicate or existing identifiers."""
        additions = tuple(agents)
        indexed = {agent.id: agent for agent in additions}
        if len(indexed) != len(additions) or not self._by_id.keys().isdisjoint(indexed):
            raise ValueError('agent registration ids must be unique')
        self._agents.extend(additions)
        self._by_id.update(indexed)

    def get(self, agent_id: str) -> AgentRegistration[Any, Any] | None:
        """Return a registration by ID without scanning the ordered catalog."""
        return self._by_id.get(agent_id)

    def __len__(self) -> int:
        return len(self._agents)

    if TYPE_CHECKING:

        @overload
        def __getitem__(self, index: int) -> AgentRegistration[Any, Any]: ...

        @overload
        def __getitem__(self, index: slice) -> Sequence[AgentRegistration[Any, Any]]: ...

    def __getitem__(self, index: int | slice) -> AgentRegistration[Any, Any] | Sequence[AgentRegistration[Any, Any]]:
        return self._agents[index]
