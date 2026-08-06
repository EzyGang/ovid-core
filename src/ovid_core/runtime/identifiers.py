from typing import Self
from uuid import UUID, uuid4

from ovid_core.models import BaseRootModel


class RunId(BaseRootModel[UUID]):
    @classmethod
    def new(cls) -> Self:
        return cls(root=uuid4())

    def __str__(self) -> str:
        return str(self.root)


class ConversationId(BaseRootModel[UUID]):
    @classmethod
    def new(cls) -> Self:
        return cls(root=uuid4())

    def __str__(self) -> str:
        return str(self.root)
