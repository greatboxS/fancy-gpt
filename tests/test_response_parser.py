import pytest

from fancy_gpt.response_parser import parse_json_object


def test_parses_plain_json_object():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_strips_code_fence():
    raw = '```json\n{"a": 1}\n```'
    assert parse_json_object(raw) == {"a": 1}


def test_strips_bare_code_fence():
    raw = '```\n{"a": 1}\n```'
    assert parse_json_object(raw) == {"a": 1}


def test_rejects_truncated_json():
    with pytest.raises(ValueError, match="valid JSON value"):
        parse_json_object('{"intent": ["consult"],')


def test_rejects_non_object_json():
    with pytest.raises(ValueError, match="one JSON object"):
        parse_json_object("[1, 2, 3]")


def test_rejects_prose_wrapped_json():
    with pytest.raises(ValueError, match="valid JSON value"):
        parse_json_object('Sure, here is the result:\n{"a": 1}')
