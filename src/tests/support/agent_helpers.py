from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.test import TestModel

from ovid_core import AgentFactory
from ovid_core.adapters.pydantic_ai import PydanticAIAgentCompiler
from ovid_core.config import ModelConfig, OvidConfig
from ovid_core.routing import ModelCapabilities, ModelHandle, ModelRouter, ModelRuntime


class RuntimeFactory:
    def __init__(self, runtimes: dict[str, ModelRuntime]) -> None:
        self.runtimes = runtimes

    async def build(self, *, model_id: str, config: ModelConfig) -> ModelHandle:
        del config
        runtime = self.runtimes[model_id]

        return ModelHandle(
            model_id=model_id,
            model_name=runtime.model_name,
            capabilities=ModelCapabilities(
                tools=True,
                json_schema_output=True,
                json_object_output=True,
                image_output=False,
                thinking=False,
            ),
            runtime=runtime,
        )


class UnsupportedRuntime:
    @property
    def model_name(self) -> str:
        return 'unsupported'


def failing_request(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages, info
    raise ModelAPIError('failing', 'provider-secret')


def agent_factory(
    runtimes: dict[str, ModelRuntime],
    *,
    route: bool = False,
    compiler: PydanticAIAgentCompiler | None = None,
) -> AgentFactory:
    models = {model_id: {'provider': 'test', 'model': model_id} for model_id in runtimes}
    routes = {'answer': {'models': tuple(runtimes)}} if route else {}
    config = OvidConfig.model_validate({'models': models, 'routes': routes})
    router = ModelRouter(config=config, factory=RuntimeFactory(runtimes))

    return AgentFactory(router=router, compiler=compiler or PydanticAIAgentCompiler())


def structured_test_model() -> TestModel:
    return TestModel(
        call_tools=['add'],
        custom_output_args={'value': 'done'},
        model_name='working',
    )
