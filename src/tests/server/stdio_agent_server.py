import asyncio

from pydantic import JsonValue

from ovid_core.server import AuthorizationResult, CommandRegistration, RequestContext, create_stdio_server
from tests.server.server_helpers import build_registration


async def authorize(_: RequestContext, __: str) -> AuthorizationResult:
    return AuthorizationResult(allowed=True, principal='subprocess')


async def inspect(
    context: RequestContext,
    authorization: AuthorizationResult,
    arguments: JsonValue,
) -> JsonValue:
    return {
        'arguments': arguments,
        'method': context.method,
        'principal': authorization.principal,
    }


async def main() -> None:
    registration = await build_registration()
    server = create_stdio_server(
        agents=(registration,),
        commands=(CommandRegistration(id='inspect', description='Inspect context.', handler=inspect),),
        authorize=authorize,
    )
    await server.run()


if __name__ == '__main__':
    asyncio.run(main())
