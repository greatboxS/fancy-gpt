class BrowserAutomationError(RuntimeError):
    """Base error for browser automation failures."""


class BrowserNotAuthenticatedError(BrowserAutomationError):
    pass


class BrowserUiDriftError(BrowserAutomationError):
    """Expected ChatGPT UI contract cannot be proven; fail closed."""


class BrowserTurnAmbiguousError(BrowserAutomationError):
    """A response cannot be uniquely bound to the submitted turn."""


class BrowserTurnTimeoutError(BrowserAutomationError):
    pass


class BrowserCapacityError(BrowserAutomationError):
    pass


class BrowserPromptTooLargeError(BrowserAutomationError):
    pass
