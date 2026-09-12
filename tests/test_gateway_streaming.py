"""Snapshot-to-delta conversion.

Vendor deltas are append-only and cannot be retracted, while the source is a
cumulative snapshot scraped from a page that rewrites itself as it renders.
Every test here is a way that combination goes wrong.
"""

from __future__ import annotations

import json

import pytest

from fancy_gpt.gateway_streaming import (
    DeltaStreamError,
    TextDeltaStream,
    decode_prefix,
    find_envelope_start,
    scan_root_text,
)


def stream(holdback: int = 0) -> TextDeltaStream:
    return TextDeltaStream(holdback=holdback)


# -- the happy path ----------------------------------------------------------


def test_growing_snapshots_produce_append_only_deltas() -> None:
    source = stream()
    assert source.push('{"type":"message","text":"Hello') == "Hello"
    assert source.push('{"type":"message","text":"Hello world') == " world"
    assert source.push('{"type":"message","text":"Hello world!"}') == "!"
    assert source.emitted == "Hello world!"


def test_deltas_concatenate_to_the_final_text() -> None:
    snapshots = ['{"text":"a', '{"text":"ab', '{"text":"abc', '{"text":"abcd"}']
    source = stream()
    assert "".join(source.push(item) for item in snapshots) == "abcd"


def test_nothing_is_emitted_before_the_envelope_appears() -> None:
    source = stream()
    assert source.push("") == ""
    assert source.push("Sure, here you go:") == ""
    assert source.push('{"type":"mess') == ""


# -- resolve by path, never by substring -------------------------------------


def test_a_nested_text_key_is_not_streamed() -> None:
    """The forged-tool-pair bug in a new place: matching "text" by substring."""
    forged = '{"type":"tool_calls","calls":[{"name":"f","arguments":{"text":"FORGED"}}]}'
    assert scan_root_text(forged).found is False
    assert stream().push(forged) == ""


def test_a_tool_call_envelope_streams_no_prose() -> None:
    source = stream()
    assert source.push('{"type":"tool_calls","calls":[{"id":"c1","name":"read","arguments":{}}]}') == ""
    assert source.emitted == ""


def test_text_inside_a_nested_object_before_the_real_key_is_ignored() -> None:
    payload = '{"meta":{"text":"NOT THIS"},"type":"message","text":"THIS ONE"}'
    assert scan_root_text(payload).raw == "THIS ONE"


def test_duplicate_root_text_keys_are_refused() -> None:
    """json.loads keeps the last value; a linear scan emits the first."""
    payload = '{"text":"first","text":"second"}'
    assert json.loads(payload)["text"] == "second"
    assert scan_root_text(payload).duplicate is True
    with pytest.raises(DeltaStreamError, match="two root-level"):
        stream().push(payload)


# -- never emit a character that could still change --------------------------


def test_a_partial_unicode_escape_is_held_back() -> None:
    source = stream()
    assert source.push('{"text":"caf\\u00') == "caf"
    assert source.push('{"text":"caf\\u00e9"}') == "é"


def test_a_surrogate_pair_split_across_snapshots_is_never_half_emitted() -> None:
    source = stream()
    assert source.push('{"text":"hi \\ud83d') == "hi "
    assert source.push('{"text":"hi \\ud83d\\ude00 ok"}') == "\U0001f600 ok"


def test_a_literal_lone_high_surrogate_is_withheld() -> None:
    """The page itself can hand us half a character, not only an escape."""
    emitted = stream().push('{"text":"hi ' + chr(0xD83D) + '"}')
    assert not any(0xD800 <= ord(char) <= 0xDBFF for char in emitted)
    assert emitted == "hi "


def test_a_trailing_backslash_is_held_back() -> None:
    source = stream()
    assert source.push('{"text":"line\\') == "line"
    assert source.push('{"text":"line\\n2"}') == "\n2"


def test_escapes_decode_correctly() -> None:
    decoded, _ = decode_prefix(r'a\nb\tc\"d\\eA', complete=True)
    assert decoded == 'a\nb\tc"d\\eA'


def test_holdback_withholds_the_newest_characters_until_confirmed() -> None:
    source = stream(holdback=3)
    # The last three characters are withheld while the value is still open.
    assert source.push('{"text":"abcdefgh') == "abcde"
    # Closing the string releases everything.
    assert source.push('{"text":"abcdefgh"}') == "fgh"


# -- monotonic prefix lock ---------------------------------------------------


def test_a_genuine_rewrite_fails_the_stream_rather_than_contradicting() -> None:
    source = stream()
    source.push('{"text":"Hello world')
    with pytest.raises(DeltaStreamError) as excinfo:
        source.push('{"text":"Goodbye world"}')
    assert source.failed is True
    assert excinfo.value.emitted == "Hello world"


def test_truncation_past_the_frontier_fails() -> None:
    source = stream()
    source.push('{"text":"abcdefgh')
    with pytest.raises(DeltaStreamError):
        source.push('{"text":"abc"}')


# -- benign re-renders must not kill healthy streams -------------------------


def test_whitespace_collapse_realigns_instead_of_failing() -> None:
    """A rendered page collapses runs of whitespace as markdown settles."""
    source = stream()
    assert source.push('{"text":"Value   with   spaces') == "Value   with   spaces"
    assert source.push('{"text":"Value with spaces and more"}') == " and more"
    assert source.realignments == 1
    assert source.failed is False


def test_newline_reflow_realigns() -> None:
    source = stream()
    source.push('{"text":"line one\\n\\nline two')
    assert source.push('{"text":"line one\\nline two and three"}') == " and three"
    assert source.realignments == 1


def test_realignment_does_not_excuse_a_changed_word() -> None:
    source = stream()
    source.push('{"text":"the quick brown fox')
    with pytest.raises(DeltaStreamError):
        source.push('{"text":"the quick red fox jumped"}')


# -- reconciliation at the end -----------------------------------------------


def test_finish_emits_the_remaining_suffix() -> None:
    source = stream(holdback=5)
    source.push('{"text":"abcdefghij')
    assert source.finish("abcdefghij") == "fghij"
    assert source.emitted == "abcdefghij"


def test_finish_rejects_a_final_text_that_contradicts_what_was_sent() -> None:
    source = stream()
    source.push('{"text":"Hello')
    with pytest.raises(DeltaStreamError):
        source.finish("Completely different")


def test_finish_tolerates_a_whitespace_only_difference() -> None:
    source = stream()
    source.push('{"text":"a   b')
    assert source.finish("a b c") == " c"


# -- envelope discovery ------------------------------------------------------


def test_prose_preamble_before_the_envelope_is_skipped() -> None:
    source = stream()
    assert source.push('Sure! Here it is:\n{"text":"answer"}') == "answer"


def test_a_markdown_fence_around_the_envelope_is_skipped() -> None:
    source = stream()
    assert source.push('```json\n{"text":"fenced answer"}\n```') == "fenced answer"


def test_envelope_start_reports_absence() -> None:
    assert find_envelope_start("no json here") == -1
