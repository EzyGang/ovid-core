from typing import cast

from pydantic_ai import Agent, InstrumentationSettings
from pydantic_ai.agent import AgentRetries
from pydantic_ai.models import Model

from ovid_core.adapters.pydantic_ai._agent_runtime import PydanticAIAgentRuntime
from ovid_core.adapters.pydantic_ai.extensions import adapt_agent_extensions
from ovid_core.agents import AgentDefinition, AgentRuntime
from ovid_core.errors import AgentConstructionError, OvidCoreError
from ovid_core.routing.models import ResolvedModel


class DefaultAgentCompiler:
    def compile[Deps, Output](
        self,
        definition: AgentDefinition[Deps, Output],
        resolved: ResolvedModel,
    ) -> AgentRuntime[Deps, Output]:
        runtime = resolved.handle.runtime
        if not isinstance(runtime, Model):
            raise AgentConstructionError('Resolved model is not compatible with the Pydantic AI adapter')

        try:
            extensions = adapt_agent_extensions(
                definition.capabilities,
                definition.toolsets,
                definition.hooks,
                tool_approval=definition.tool_approval,
            )
            policy = definition.policy
            agent = Agent[Deps, Output](
                runtime,
                output_type=definition.output_type,
                instructions=definition.instructions,
                deps_type=definition.deps_type,
                retries=cast(AgentRetries, policy.retries.model_dump()),
                toolsets=extensions.toolsets,
                capabilities=extensions.capabilities,
                end_strategy=policy.end_strategy,
                tool_timeout=policy.tool_timeout_seconds,
                max_concurrency=policy.max_concurrency,
            )

            observability = definition.observability
            if observability.enabled:
                agent.instrument = InstrumentationSettings(
                    include_binary_content=observability.include_content,
                    include_content=observability.include_content,
                    include_model_request_parameters=observability.include_content,
                )
        except OvidCoreError:
            raise
        except Exception as error:
            raise AgentConstructionError('Pydantic AI agent construction failed') from error

        return PydanticAIAgentRuntime(agent=agent, policy=policy)
