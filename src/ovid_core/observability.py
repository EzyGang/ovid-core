from ovid_core.models import BaseModel


class ObservabilityConfig(BaseModel):
    enabled: bool = False
    include_content: bool = False
