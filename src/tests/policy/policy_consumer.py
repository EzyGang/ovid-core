from ovid_core import AgentDefinition, AgentRunPolicy, BaseModel, ObservabilityConfig, OvidAgent
from ovid_core.capabilities import BaseCapability, CapabilityContributions
from ovid_core.routing import ModelRef
from ovid_core.runtime import RunResult
from ovid_core.tools import BaseTool, ToolExecutionContext, ToolResult
from ovid_core.usage import UsageTracker


class DelegateArgs(BaseModel):
    prompt: str


class DelegateResult(ToolResult):
    output: str


class DelegateTool(BaseTool[None, DelegateArgs, DelegateResult]):
    id = 'delegate'
    description = 'Delegate work to a child agent.'
    args_type = DelegateArgs
    result_type = DelegateResult

    def __init__(self, *, child: OvidAgent[None, str], tracker: UsageTracker) -> None:
        self._child = child
        self._tracker = tracker
        self.child_result: RunResult[str] | None = None

    async def execute(
        self,
        context: ToolExecutionContext[None],
        arguments: DelegateArgs,
    ) -> DelegateResult:
        child_tracker = self._tracker.create_child()
        self.child_result = await self._child.run(
            arguments.prompt,
            deps=None,
            usage_tracker=child_tracker,
        )

        return DelegateResult(content=self.child_result.output, output=self.child_result.output)


def child_definition(
    *,
    policy: AgentRunPolicy = AgentRunPolicy(),
    observability: ObservabilityConfig = ObservabilityConfig(),
) -> AgentDefinition[None, str]:
    return AgentDefinition(
        model=ModelRef(name='child'),
        deps_type=type(None),
        output_type=str,
        policy=policy,
        observability=observability,
    )


def parent_definition(
    tool: DelegateTool,
    *,
    policy: AgentRunPolicy = AgentRunPolicy(),
    observability: ObservabilityConfig = ObservabilityConfig(),
) -> AgentDefinition[None, str]:
    capability = BaseCapability[None](
        id='delegation',
        contributions=CapabilityContributions(tools=(tool,)),
    )

    return AgentDefinition(
        model=ModelRef(name='parent'),
        deps_type=type(None),
        output_type=str,
        capabilities=(capability,),
        policy=policy,
        observability=observability,
    )
