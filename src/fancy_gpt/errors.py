class FancyGPTError(Exception):
    """Base domain error."""


class InvalidStateError(FancyGPTError):
    pass


class ContextSecurityError(FancyGPTError):
    pass


class ContextTooLargeError(FancyGPTError):
    pass


class ContextRequirementError(FancyGPTError):
    pass


class CatalogError(FancyGPTError):
    pass
