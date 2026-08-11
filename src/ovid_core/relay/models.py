from typing import Annotated, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from ovid_core.models import BaseModel, BaseRootModel


type _OpaqueAddress = Annotated[str, Field(min_length=1)]


class RelayAddress(BaseRootModel[_OpaqueAddress]):
    def __str__(self) -> str:
        return self.root


class RelayIdentity(BaseModel):
    address: RelayAddress
    display_name: str


class RelayMessageId(BaseRootModel[UUID]):
    @classmethod
    def new(cls) -> Self:
        return cls(root=uuid4())

    def __str__(self) -> str:
        return str(self.root)


class RelayMessage(BaseModel):
    id: RelayMessageId
    sender: RelayAddress
    recipient: RelayAddress
    content: str
    sent_at: AwareDatetime
    reply_to: RelayMessageId | None = None


class RelayReceipt(BaseModel):
    message_id: RelayMessageId
    recipient: RelayAddress
    accepted_at: AwareDatetime


class RelayContact(BaseModel):
    address: RelayAddress
    display_name: str
