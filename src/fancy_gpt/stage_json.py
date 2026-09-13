"""Re-ask a model whose reply was not valid JSON, once, in the same place.

Every stage of this system ends the same way: a browser turn produces text,
and that text has to parse. The turns are expensive -- a real page, a real
model, tens of seconds each -- and a two-pass review spends two of them before
anything is validated. Hanging all of that on the model escaping every quote
correctly the first time is not a design, it is a coin toss: one unescaped
`"` inside one string threw away a 26KB planner reply that was otherwise
entirely correct, twice in a row on the same wording.

So the re-ask lives here rather than in any one caller. The review engine, the
focused path and the team agent each used to parse in their own way, which
meant robustness had to be remembered three times and was in fact present in
none of them.

Nothing is repaired by guesswork. A heuristic that edits a reply into shape
cannot tell the difference between fixing an escape and inventing a value, and
a wrong value that parses is worse than a reply that does not: the first is
acted on. The model is asked instead, in the same conversation so that it is
correcting its own words rather than answering again blind.
"""

from __future__ import annotations

import os
from typing import Callable, TypeVar

from .providers.base import AutomaticModelProvider

T = TypeVar("T")


def repair_attempts() -> int:
    """How many times a stage may be re-asked. Bounded on purpose.

    A model that cannot produce the shape twice will not produce it on the
    tenth try, and every attempt costs a real turn in a real browser.
    """
    try:
        return max(0, int(os.getenv("FANCY_GPT_JSON_REPAIR_ATTEMPTS", "2")))
    except ValueError:
        return 2


# A reply quoted back into the repair prompt is capped, because the prompt has
# to fit in a composer. Past this, the model is asked without it rather than the
# turn being failed by an oversized prompt.
QUOTE_BACK_LIMIT = 24000


def repair_prompt(error: str, previous: str | None = None) -> str:
    quoted = ""
    if previous:
        quoted = (
            "\n\nThis is the reply that could not be parsed. "
            "Correct it and send only the corrected version:\n\n"
            + previous
        )
    return (
        f"Your previous reply could not be parsed: {error}.\n\n"
        "Send that same reply again with no change to its content or meaning, "
        "as one valid JSON value and nothing else: no prose before or after it, "
        "and no code fence around it.\n"
        "The usual cause is a double quote inside a string that was not escaped. "
        'Every " inside a string must be written as \\", and every backslash as \\\\.\n'
        "If your reply included fenced blocks after the JSON, repeat them exactly as they were."
        + quoted
    )


def parse_or_reask(
    provider: AutomaticModelProvider,
    model_request,
    response,
    parse: Callable[[str], T],
    *,
    on_progress: Callable[[str], None] | None = None,
    on_response: Callable[[object, int], None] | None = None,
    attempts: int | None = None,
) -> tuple[T, object]:
    """Parse `response`, re-asking the model while `parse` rejects its text.

    `parse` is the caller's own parser, so each stage keeps its own contract --
    one JSON object, or a JSON object plus fenced verbatim blocks -- and only
    the re-asking is shared. `on_response` is called with every reply and its
    attempt number, so a caller can persist the failed ones: a malformed reply
    nobody kept is a failure nobody can diagnose.

    Returns the parsed value together with the response it came from, because
    a later attempt carries its own conversation binding and identity.
    """
    limit = repair_attempts() if attempts is None else max(0, attempts)
    current = response
    last_error: ValueError | None = None
    for attempt in range(limit + 1):
        if on_response is not None:
            on_response(current, attempt)
        try:
            return parse(current.raw_text), current
        except ValueError as exc:
            last_error = exc
            if attempt == limit:
                break
        # The model can only correct its own reply if it can still see it. When
        # the turn ran in a conversation we can return to, continue it. When it
        # did not -- a fresh or temporary turn never gets an id, which is the
        # default for focused questions -- the reply is quoted into the prompt
        # instead. Asking to continue an id we do not have would open a brand
        # new chat: the model would be asked to correct a reply it has never
        # seen, and a deliberately temporary turn would leave a saved thread
        # behind it.
        conversation_id = getattr(current, "conversation_id", None)
        metadata = dict(model_request.metadata)
        if conversation_id:
            metadata["conversation_id"] = conversation_id
            metadata["conversation_mode"] = "persistent"
        quote_back = None
        if not conversation_id and len(current.raw_text) <= QUOTE_BACK_LIMIT:
            quote_back = current.raw_text
        retry = model_request.model_copy(
            update={"prompt": repair_prompt(str(last_error), quote_back), "metadata": metadata}
        )
        current = provider.execute(retry, on_progress=on_progress)
    plural = "" if limit == 1 else "s"
    raise ValueError(f"{last_error} (after {limit} re-ask{plural})") from last_error
