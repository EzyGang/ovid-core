from dataclasses import dataclass, field

from ovid_core.capabilities.base import BaseCapability, CapabilityContributions
from ovid_core.relay.contracts import RelayConnection
from ovid_core.relay.tools import (
    RelayContactsTool,
    RelayPendingTool,
    RelaySendTool,
    RelayToolDescriptions,
    RelayWaitTool,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RelayCapability[Deps](BaseCapability[Deps]):
    id: str = field(default='relay', init=False)
    description: str = field(default='Send, receive, inspect, and address Relay messages.', init=False)
    defer_loading: bool = field(default=False, init=False)
    connection: RelayConnection
    tool_descriptions: RelayToolDescriptions = RelayToolDescriptions()
    contributions: CapabilityContributions[Deps] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        tools = (
            RelaySendTool[Deps](self.connection, description=self.tool_descriptions.send),
            RelayWaitTool[Deps](self.connection, description=self.tool_descriptions.wait),
            RelayPendingTool[Deps](self.connection, description=self.tool_descriptions.pending),
            RelayContactsTool[Deps](self.connection, description=self.tool_descriptions.contacts),
        )
        object.__setattr__(self, 'contributions', CapabilityContributions(tools=tools))
