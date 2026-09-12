from __future__ import annotations

import pytest

from fancy_gpt.tunnels import TunnelCapabilities, TunnelCompositionValidator, TunnelScope, TunnelSpec, TunnelRuntime, TunnelTransport
from fancy_gpt.tunnels.factory import TunnelDriverFactory
from fancy_gpt.tunnels.health import TunnelLayerInspector
from fancy_gpt.web.models import LayerKind
from fancy_gpt.web.runtime import RuntimeRegistry
from fancy_gpt.web.sites import SiteRegistry
from fancy_gpt.web.transport import TransportRegistry


def make_spec(**overrides):
    data = dict(
        id="test-extension-remote",
        description="test",
        runtime=TunnelRuntime.EXTENSION,
        transport=TunnelTransport.WEBSOCKET,
        scope=TunnelScope.REMOTE,
        browser="chrome",
        endpoint="ws://127.0.0.1:9999",
        capabilities=TunnelCapabilities(
            automatic=True,
            remote_capable=True,
            existing_session=True,
            persistent_session=True,
            requires_extension=True,
            requires_bridge=True,
            supports_ssh_forward=True,
            browser_families=["chrome"],
        ),
    )
    data.update(overrides)
    return TunnelSpec.model_validate(data)


def test_site_layer_contract_is_independent() -> None:
    site = SiteRegistry().get("chatgpt")
    assert site.accepts_host("chatgpt.com")
    assert "model.turn" in site.operations
    assert site.supports_fresh_conversation


def test_runtime_layer_contracts_are_independent() -> None:
    runtimes = RuntimeRegistry()
    extension = runtimes.get("extension")
    assert extension.supports_browser("chrome")
    assert extension.supports_browser("edge")
    assert extension.supports_browser("firefox")
    assert set(extension.supported_transports) == {"native-messaging", "websocket"}
    playwright = runtimes.get("playwright")
    assert not playwright.supports_browser("chrome")
    assert playwright.owns_browser_process


def test_transport_layer_contracts_are_independent() -> None:
    transports = TransportRegistry()
    websocket = transports.get("websocket")
    assert websocket.remote_capable
    assert websocket.supports_ssh_forward
    native = transports.get("native-messaging")
    assert native.requires_native_host
    assert not native.remote_capable


def test_composition_layer_rejects_invalid_cross_layer_tuple() -> None:
    validator = TunnelCompositionValidator()
    good = make_spec()
    validator.validate(good)

    invalid = make_spec(
        id="bad-playwright-native",
        runtime=TunnelRuntime.PLAYWRIGHT,
        transport=TunnelTransport.NATIVE_MESSAGING,
        scope=TunnelScope.LOCAL,
        browser="firefox",
        capabilities=TunnelCapabilities(
            automatic=True,
            requires_native_host=True,
            browser_families=["firefox"],
        ),
    )
    with pytest.raises(ValueError, match="does not support transport"):
        validator.validate(invalid)


def test_layer_inspector_keeps_site_out_of_tunnel_layers() -> None:
    layers = TunnelLayerInspector().inspect(make_spec())
    assert {item.layer for item in layers} == {
        LayerKind.RUNTIME,
        LayerKind.TRANSPORT,
        LayerKind.COMPOSITION,
    }
    assert all(item.state == "healthy" for item in layers)


def test_driver_factory_is_runtime_boundary_and_injectable() -> None:
    sentinel = object()
    factory = TunnelDriverFactory(builders={"extension": lambda _spec: sentinel})
    assert factory.build(make_spec()) is sentinel


def test_per_tunnel_endpoint_and_token_overrides(tmp_path, monkeypatch) -> None:
    spec = make_spec()
    key = "FANCY_GPT_TUNNEL_TEST_EXTENSION_REMOTE_ENDPOINT"
    token_key = "FANCY_GPT_TUNNEL_TEST_EXTENSION_REMOTE_TOKEN_FILE"
    token_file = tmp_path / "token"
    monkeypatch.setenv(key, "ws://127.0.0.1:4567")
    monkeypatch.setenv(token_key, str(token_file))
    factory = TunnelDriverFactory(bridge_token_file=tmp_path / "default-token")
    assert factory.endpoint(spec) == "ws://127.0.0.1:4567"
    assert factory.token_file(spec) == token_file.resolve()
