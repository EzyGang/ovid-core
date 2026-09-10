from typing import Annotated

from pydantic import AwareDatetime, Field

from ovid_core.models import BaseModel, BaseRootModel, UUIDRootModel


type _OpaqueAddress = Annotated[str, Field(min_length=1)]


class RelayAddress(BaseRootModel[_OpaqueAddress]):
    def __str__(self) -> str:
        return self.root


class RelayIdentity(BaseModel):
    address: RelayAddress
    display_name: str


class RelayMessageId(UUIDRootModel):
    pass


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
