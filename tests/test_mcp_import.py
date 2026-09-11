import pytest

def test_mcp_server_imports_when_sdk_available():
    pytest.importorskip("mcp")
    import fancy_gpt.mcp_server as server
    assert server.mcp is not None
