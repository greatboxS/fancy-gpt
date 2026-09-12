"""Token estimation for the usage fields the gateway reports.

The gateway has no tokenizer for the model actually answering, because that
model is a web chat. So `usage` is an estimate - but it is not a cosmetic one:
Claude Code reads `input_tokens`/`output_tokens` to decide when to compact its
own context. An estimate that reads *low* makes a client compact too late and
overrun its real context window.

So the bias is deliberate: **over-estimate rather than under-estimate**. A
client that compacts slightly early loses a little history; a client that
compacts too late fails outright.

`chars / 4` is the naive version and is wrong in the dangerous direction: it
suits English prose but under-counts code, JSON and CJK by a wide margin.
"""

from __future__ import annotations

import unicodedata

#: Characters that are roughly one token each in every current tokenizer.
_CJK_RANGES = (
    (0x3040, 0x30FF),   # kana
    (0x3400, 0x4DBF),   # CJK extension A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xAC00, 0xD7AF),   # hangul
    (0xF900, 0xFAFF),   # CJK compatibility
)

#: Average characters per token for ordinary prose.
PROSE_CHARS_PER_TOKEN = 3.6
#: Denser text - code, JSON, markup - packs fewer characters into each token.
DENSE_CHARS_PER_TOKEN = 2.6
#: Above this share of punctuation/symbols, text is treated as dense.
DENSE_SYMBOL_RATIO = 0.12


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Estimate tokens, biased to over-count rather than under-count.

    Wide characters are counted individually; the remainder is divided by a
    characters-per-token figure chosen from how symbol-dense the text is.
    """
    if not text:
        return 0

    cjk = 0
    symbols = 0
    remaining = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
            continue
        remaining += 1
        if char.isspace():
            continue
        category = unicodedata.category(char)
        # P* is punctuation, S* is symbols: both drive up token density.
        if category.startswith("P") or category.startswith("S"):
            symbols += 1

    non_space = max(1, remaining)
    density = symbols / non_space
    chars_per_token = DENSE_CHARS_PER_TOKEN if density >= DENSE_SYMBOL_RATIO else PROSE_CHARS_PER_TOKEN
    estimate = cjk + (remaining / chars_per_token)
    # A non-empty string always costs at least one token.
    return max(1, int(estimate + 0.999))


def estimate_message_tokens(texts: list[str], *, per_message_overhead: int = 4) -> int:
    """Estimate a whole transcript, including per-message framing overhead."""
    return sum(estimate_tokens(text) + per_message_overhead for text in texts)
