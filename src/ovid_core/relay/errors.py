from ovid_core.errors import OvidCoreError


class RelayError(OvidCoreError):
    pass


class UnknownRelayRecipientError(RelayError):
    pass


class RelayCapacityError(RelayError):
    pass


class RelayUnavailableError(RelayError):
    pass


class RelayAddressInUseError(RelayError):
    pass
