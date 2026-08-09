class OvidCoreError(Exception):
    pass


class ConfigurationError(OvidCoreError):
    pass


class CredentialError(OvidCoreError):
    pass


class CodexAuthError(CredentialError):
    pass


class ProviderError(OvidCoreError):
    pass


class ModelResolutionError(OvidCoreError):
    pass


class AgentConstructionError(OvidCoreError):
    pass


class ToolError(OvidCoreError):
    pass


class PluginError(OvidCoreError):
    pass


class TransportError(OvidCoreError):
    pass
