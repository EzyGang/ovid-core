# Use Relay between agents

Relay gives selected agents direct asynchronous messaging through application-owned connections.
Relay stays inactive until you attach `RelayCapability` to an agent definition.

Use Relay for these communication paths:

- an orchestrator and a spawned agent
- an agent that reports progress
- equal agents that share a message path

Relay does not define agent roles or launch agents.
The application decides how incoming messages enter a conversation.

## Create an in-memory network

Create one `InMemoryRelay` for every group of connections that should communicate:

```python
from ovid_core.relay import InMemoryRelay, RelayAddress, RelayIdentity

relay = InMemoryRelay(capacity=100)

orchestrator_connection = relay.connection(
    RelayIdentity(
        address=RelayAddress('orchestrator'),
        display_name='Orchestrator',
    )
)
worker_connection = relay.connection(
    RelayIdentity(
        address=RelayAddress('worker'),
        display_name='Worker',
    )
)
```

Connections from different `InMemoryRelay` instances remain isolated.
Each address must be unique in one network.
Contacts are the other live connections in that network.
Relay does not assign running, idle, parent, or child status.

The in-memory backend is process-local and bounded.
A full mailbox rejects a new message with `RelayCapacityError`.
The backend never drops an accepted message silently.

## Attach the capability

Pass an already usable connection to each participating agent:

```python
from ovid_core.agents import AgentDefinition
from ovid_core.relay import RelayCapability
from ovid_core.routing.models import ModelRef

orchestrator_definition = AgentDefinition[AppDeps, Answer](
    model=ModelRef(name='primary'),
    deps_type=AppDeps,
    output_type=Answer,
    capabilities=(
        RelayCapability[AppDeps](connection=orchestrator_connection),
    ),
)
worker_definition = AgentDefinition[AppDeps, Answer](
    model=ModelRef(name='primary'),
    deps_type=AppDeps,
    output_type=Answer,
    capabilities=(
        RelayCapability[AppDeps](connection=worker_connection),
    ),
)
```

`AgentFactory` does not create, configure, start, or close Relay connections. The application keeps each connection alive for as long
as that agent should remain addressable.

## Deliver incoming messages automatically

Tools capture the sending agent's intent. The recipient connection owns message arbitration and calls an application delivery handler
when no matching `relay_wait` is active.

```python
from ovid_core.relay import RelayDisposition, RelayMessage


async def deliver_to_worker(message: RelayMessage) -> RelayDisposition:
    session = worker_sessions.current(message.recipient)
    if session is None:
        return RelayDisposition.DEFER

    accepted = await session.enqueue_relay_message(message)

    return RelayDisposition.ACKNOWLEDGE if accepted else RelayDisposition.DEFER


worker_connection.set_delivery_handler(deliver_to_worker)
```

The application can use this handler to:

- queue an aside
- steer a running loop
- append conversation history
- start an idle turn
- forward the message to a remote worker

Return `ACKNOWLEDGE` after the application accepts responsibility for the message.
Return `DEFER` when the message must remain unread.
A handler exception also leaves the message pending.
Handler work runs independently of sender acceptance.
The in-memory connection serializes handler work for each recipient.

Setting a handler later makes pending messages eligible for automatic delivery. Setting it to `None` leaves future messages pending.

## Use the Relay tools

Attaching the capability contributes four tools automatically:

| Tool | Use |
| --- | --- |
| `relay_send` | Send a message to a known address and optionally identify the message being answered |
| `relay_wait` | Wait for and consume one message, optionally filtered by sender or exact reply correlation |
| `relay_pending` | Read outstanding messages in FIFO order and consume them unless `retain=true` |
| `relay_contacts` | List addresses visible through the connection |

A typical delegation exchange is:

1. The task layer launches a worker with its Relay connection.
2. The task result gives the orchestrator the worker address.
3. The worker receives the orchestrator address through task instructions or dependencies.
4. Either agent calls `relay_send` for instructions, progress, correction, or completion.
5. Relay sends the message to a matching waiter or the application delivery handler.

A send receipt confirms only that the recipient connection accepted the message.
It does not confirm that the recipient read, processed, or answered it.

Use `reply_to` for exact correlation.
After sending message `M`, the recipient answers with `reply_to=M.id`.
The sender can call `relay_wait(reply_to=M.id)` without consuming an unrelated message.

`relay_pending(retain=true)` inspects unread messages without consuming them.
Without `retain`, the call consumes returned messages atomically.
Acknowledged messages and messages returned through `relay_wait` are no longer pending.

## Customize model-visible tool descriptions

Override only the descriptions that need application terminology:

```python
from ovid_core.relay import RelayCapability, RelayToolDescriptions

relay_capability = RelayCapability[AppDeps](
    connection=worker_connection,
    tool_descriptions=RelayToolDescriptions(
        send='Send instructions or progress to a known delegation contact.',
        wait='Wait for a correlated delegation response.',
    ),
)
```

Unspecified descriptions keep their core defaults. Put broader collaboration and delegation policy in `AgentDefinition.instructions`.

## Supply another connection implementation

The capability depends only on `RelayConnection`:

```python
from ovid_core.relay import RelayCapability, RelayConnection

connection: RelayConnection = application_relay.connection_for('worker')
relay_capability = RelayCapability[AppDeps](connection=connection)
```

A consumer connection can use:

- a broker
- a database
- an RPC service
- a remote worker

It owns identity binding, contact visibility, and mailbox storage.
It also owns automatic delivery, initialization, reconnection, and shutdown.
The required protocol does not impose a context manager or lifecycle method.

The connection must preserve the behavior in the [Relay API reference](../api/extensions.md#relay):

- waiter precedence
- exact filters
- pending-message consumption
- receipt semantics
- application delivery dispositions

## Close in-memory connections

`InMemoryRelayConnection.close()` unregisters that address.
It wakes active waiters with `RelayUnavailableError`.
It rejects later operations through the closed connection:

```python
worker_connection.close()
```

Custom connection implementations define their own lifecycle API.
The application that created the connection must close it.
