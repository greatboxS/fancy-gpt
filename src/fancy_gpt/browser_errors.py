"""Classify what the browser and the web page did wrong.

A browser-backed turn fails in ways an HTTP client never does: the user is
signed out, a consent modal swallows the click, the site shows a usage-limit
interstitial, an A/B test moved a selector, the tab is discarded under memory
pressure. Reporting all of those as one generic failure makes them
indistinguishable in a log, and - worse - some of them are silently retryable
while others will never succeed no matter how many times they are retried.

Each failure therefore carries three decisions the caller actually needs:
whether retrying can help, whether a human has to intervene, and which
protocol-level status honestly represents it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class BrowserFailure(str, Enum):
    """What actually went wrong, as specifically as we can tell."""

    AUTH_REQUIRED = "auth-required"
    SESSION_EXPIRED = "session-expired"
    RATE_LIMITED = "rate-limited"
    BLOCKED_BY_DIALOG = "blocked-by-dialog"
    COMPOSER_REJECTED = "composer-rejected"
    SELECTOR_MISSING = "selector-missing"
    ADAPTER_DRIFT = "adapter-drift"
    RESPONSE_TRUNCATED = "response-truncated"
    RESPONSE_AMBIGUOUS = "response-ambiguous"
    MALFORMED_ENVELOPE = "malformed-envelope"
    MODEL_REFUSED = "model-refused"
    PAGE_NAVIGATED = "page-navigated"
    TAB_DISCARDED = "tab-discarded"
    WORKER_EVICTED = "worker-evicted"
    OFFLINE = "offline"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailurePolicy:
    """How a failure should be handled and reported."""

    failure: BrowserFailure
    retryable: bool
    needs_user_action: bool
    #: Status for the caller's own protocol envelope.
    http_status: int
    guidance: str


_POLICIES: dict[BrowserFailure, FailurePolicy] = {
    BrowserFailure.AUTH_REQUIRED: FailurePolicy(
        BrowserFailure.AUTH_REQUIRED, False, True, 401,
        "sign in to the site in the automated browser profile, then retry",
    ),
    BrowserFailure.SESSION_EXPIRED: FailurePolicy(
        BrowserFailure.SESSION_EXPIRED, False, True, 401,
        "the site signed the browser out mid-turn; sign in again and retry",
    ),
    BrowserFailure.RATE_LIMITED: FailurePolicy(
        BrowserFailure.RATE_LIMITED, True, False, 429,
        "the site is rate limiting this account; retry after the stated cooldown",
    ),
    BrowserFailure.BLOCKED_BY_DIALOG: FailurePolicy(
        BrowserFailure.BLOCKED_BY_DIALOG, True, True, 409,
        "a consent or announcement dialog is covering the composer; dismiss it once",
    ),
    BrowserFailure.COMPOSER_REJECTED: FailurePolicy(
        BrowserFailure.COMPOSER_REJECTED, True, False, 502,
        "the page did not accept the injected prompt; retrying usually succeeds",
    ),
    BrowserFailure.SELECTOR_MISSING: FailurePolicy(
        BrowserFailure.SELECTOR_MISSING, False, True, 502,
        "the site's markup no longer matches this adapter; the adapter needs updating",
    ),
    BrowserFailure.ADAPTER_DRIFT: FailurePolicy(
        BrowserFailure.ADAPTER_DRIFT, False, True, 502,
        "the browser is running a different adapter build; re-export and reload the extension",
    ),
    BrowserFailure.RESPONSE_TRUNCATED: FailurePolicy(
        BrowserFailure.RESPONSE_TRUNCATED, True, False, 502,
        "the reply stopped early; retry, or the turn may need continuing",
    ),
    BrowserFailure.RESPONSE_AMBIGUOUS: FailurePolicy(
        BrowserFailure.RESPONSE_AMBIGUOUS, True, False, 502,
        "more than one new reply appeared, so none could be attributed to this turn",
    ),
    BrowserFailure.MALFORMED_ENVELOPE: FailurePolicy(
        BrowserFailure.MALFORMED_ENVELOPE, True, False, 502,
        "the model did not answer with the required JSON envelope",
    ),
    BrowserFailure.MODEL_REFUSED: FailurePolicy(
        BrowserFailure.MODEL_REFUSED, False, False, 422,
        "the site's model declined this request; retrying the same prompt will not help",
    ),
    BrowserFailure.PAGE_NAVIGATED: FailurePolicy(
        BrowserFailure.PAGE_NAVIGATED, True, False, 502,
        "the page navigated or reloaded during the turn",
    ),
    BrowserFailure.TAB_DISCARDED: FailurePolicy(
        BrowserFailure.TAB_DISCARDED, True, False, 502,
        "the browser discarded the automation tab, most likely under memory pressure",
    ),
    BrowserFailure.WORKER_EVICTED: FailurePolicy(
        BrowserFailure.WORKER_EVICTED, True, False, 502,
        "the extension's background worker was evicted mid-turn",
    ),
    BrowserFailure.OFFLINE: FailurePolicy(
        BrowserFailure.OFFLINE, True, False, 503,
        "the browser reported no network connection",
    ),
    BrowserFailure.TIMEOUT: FailurePolicy(
        BrowserFailure.TIMEOUT, True, False, 504,
        "the reply did not settle within the configured timeout",
    ),
    BrowserFailure.UNKNOWN: FailurePolicy(
        BrowserFailure.UNKNOWN, True, False, 502,
        "the browser turn failed for an unrecognised reason; see the turn trace",
    ),
}


def policy_for(failure: BrowserFailure) -> FailurePolicy:
    return _POLICIES[failure]


#: Signals in the order they are tested. Earlier entries are more specific, so a
#: message mentioning both a limit and a sign-in prompt is reported as the limit.
_SIGNALS: tuple[tuple[BrowserFailure, re.Pattern[str]], ...] = (
    (BrowserFailure.ADAPTER_DRIFT, re.compile(r"extension is running|adapter drift|different build", re.I)),
    (BrowserFailure.RATE_LIMITED, re.compile(
        r"rate limit|you've reached|you have reached|usage (?:cap|limit)|try again (?:in|later)|too many requests|429", re.I)),
    (BrowserFailure.SESSION_EXPIRED, re.compile(r"session (?:has )?expired|logged out|401|403|unauthor", re.I)),
    (BrowserFailure.AUTH_REQUIRED, re.compile(r"sign in|log in|login|not signed in|authenticat", re.I)),
    (BrowserFailure.BLOCKED_BY_DIALOG, re.compile(r"dialog|modal|consent|cookie banner|overlay|intercept", re.I)),
    (BrowserFailure.OFFLINE, re.compile(r"offline|no network|connection lost|net::err", re.I)),
    (BrowserFailure.TAB_DISCARDED, re.compile(r"discard|tab was unloaded|no tab with id", re.I)),
    (BrowserFailure.WORKER_EVICTED, re.compile(
        r"extension context invalidated|message port closed|service worker", re.I)),
    (BrowserFailure.PAGE_NAVIGATED, re.compile(r"navigat|reload|execution context was destroyed", re.I)),
    (BrowserFailure.RESPONSE_AMBIGUOUS, re.compile(r"ambiguous", re.I)),
    (BrowserFailure.COMPOSER_REJECTED, re.compile(r"composer|send (?:button|control)|did not accept", re.I)),
    (BrowserFailure.SELECTOR_MISSING, re.compile(r"selector|unavailable|not found|no element", re.I)),
    (BrowserFailure.RESPONSE_TRUNCATED, re.compile(r"truncat|incomplete|continue generating|empty", re.I)),
    (BrowserFailure.MALFORMED_ENVELOPE, re.compile(r"invalid response envelope|json|envelope", re.I)),
    (BrowserFailure.MODEL_REFUSED, re.compile(r"refus|cannot (?:help|assist|fulfil)|declin", re.I)),
    (BrowserFailure.TIMEOUT, re.compile(r"timed out|timeout", re.I)),
)


def classify(message: str, *, reported: str | None = None) -> FailurePolicy:
    """Work out what a browser-side failure actually was.

    ``reported`` is a code the extension supplied directly and is trusted over
    the free-text message, because the page is the only place some of these are
    observable at all.
    """
    if reported:
        try:
            return policy_for(BrowserFailure(reported))
        except ValueError:
            pass
    text = message or ""
    for failure, pattern in _SIGNALS:
        if pattern.search(text):
            return policy_for(failure)
    return policy_for(BrowserFailure.UNKNOWN)


#: Exception types that identify a failure regardless of their wording. An
#: adapter may word a timeout any way it likes; the type still says what it is.
_BY_TYPE: tuple[tuple[type[BaseException], BrowserFailure], ...] = (
    (TimeoutError, BrowserFailure.TIMEOUT),
    (ConnectionError, BrowserFailure.OFFLINE),
)


def classify_exception(exc: BaseException) -> FailurePolicy:
    """Classify a raised failure, preferring its type over its wording."""
    reported = getattr(exc, "reported_failure", None)
    if reported:
        try:
            return policy_for(BrowserFailure(reported))
        except ValueError:
            pass
    # The message is checked first so a specific signal (a usage limit, a
    # sign-in wall) is not lost to a generic wrapper type.
    by_message = classify(str(exc))
    if by_message.failure is not BrowserFailure.UNKNOWN:
        return by_message
    for kind, failure in _BY_TYPE:
        if isinstance(exc, kind):
            return policy_for(failure)
    return by_message


class BrowserTurnError(RuntimeError):
    """A browser turn failed, with the reason identified."""

    def __init__(self, message: str, policy: FailurePolicy) -> None:
        super().__init__(message)
        self.policy = policy

    @property
    def failure(self) -> BrowserFailure:
        return self.policy.failure

    @property
    def retryable(self) -> bool:
        return self.policy.retryable

    @property
    def needs_user_action(self) -> bool:
        return self.policy.needs_user_action
