from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TransportContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    scopes: tuple[str, ...]
    remote_capable: bool = False
    requires_bridge: bool = False
    requires_native_host: bool = False
    supports_ssh_forward: bool = False


class TransportRegistry:
    def __init__(self, contracts: list[TransportContract] | None = None) -> None:
        contracts = contracts or [
            TransportContract(
                id="native-messaging",
                scopes=("local",),
                requires_bridge=True,
                requires_native_host=True,
            ),
            TransportContract(
                id="websocket",
                scopes=("local", "remote"),
                remote_capable=True,
                requires_bridge=True,
                supports_ssh_forward=True,
            ),
            TransportContract(id="local-process", scopes=("local",)),
            TransportContract(id="cdp", scopes=("local", "remote"), remote_capable=True),
            TransportContract(id="human", scopes=("local", "remote", "any"), remote_capable=True),
            TransportContract(id="in-memory", scopes=("local",)),
        ]
        self._items = {item.id: item for item in contracts}

    def get(self, transport_id: str) -> TransportContract:
        try:
            return self._items[transport_id]
        except KeyError as exc:
            raise KeyError(f"unknown browser transport: {transport_id}") from exc

    def all(self) -> list[TransportContract]:
        return sorted(self._items.values(), key=lambda item: item.id)
