"""Capability negotiation for the model gateway.

The gateway speaks three protocols that can all express images, audio and file
attachments, but it delivers turns by typing into a real web composer. What the
*protocol* can express is therefore not what the *browser adapter* can deliver.

Advertised capability is the intersection of four axes:

    protocol  x  model alias  x  site  x  browser adapter

Anything a caller sends that falls outside that intersection is rejected before
submit. It is never dropped, and never flattened into a text placeholder that
would be reported back as success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class Modality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    VIDEO = "video"


class UnsupportedModalityError(ValueError):
    """A request carried content the resolved route cannot actually deliver."""

    def __init__(self, modality: Modality, *, protocol: str, site: str, model: str, where: str) -> None:
        super().__init__(
            f"{modality.value} content in {where} is not supported by the "
            f"{site} browser adapter for model {model!r} over the {protocol} protocol; "
            "the gateway rejects it rather than silently dropping the attachment"
        )
        self.modality = modality
        self.protocol = protocol
        self.site = site
        self.model = model
        self.where = where


#: What each wire protocol is able to express on input.
PROTOCOL_MODALITIES: dict[str, frozenset[Modality]] = {
    "openai": frozenset({Modality.TEXT, Modality.IMAGE, Modality.FILE, Modality.AUDIO}),
    "anthropic": frozenset({Modality.TEXT, Modality.IMAGE, Modality.FILE}),
    "gemini": frozenset({Modality.TEXT, Modality.IMAGE, Modality.FILE, Modality.AUDIO, Modality.VIDEO}),
}


@dataclass(frozen=True)
class AdapterCapability:
    """What a site's browser automation can actually put into the composer.

    Both supported sites are currently text-only: the automation types into a
    contenteditable composer and has no attachment-upload path. When upload
    support lands this becomes a data change, not a code change.
    """

    site: str
    input_modalities: frozenset[Modality] = field(default_factory=lambda: frozenset({Modality.TEXT}))
    output_modalities: frozenset[Modality] = field(default_factory=lambda: frozenset({Modality.TEXT}))
    supports_tool_calls: bool = True
    supports_streaming: bool = False
    notes: str = ""


ADAPTER_CAPABILITIES: dict[str, AdapterCapability] = {
    "chatgpt": AdapterCapability(
        site="chatgpt",
        notes="Text composer automation; no attachment upload path is implemented.",
    ),
    "gemini": AdapterCapability(
        site="gemini",
        notes="Text composer automation; no attachment upload path is implemented.",
    ),
}


@dataclass(frozen=True)
class ResolvedCapability:
    protocol: str
    site: str
    model: str
    input_modalities: frozenset[Modality]
    output_modalities: frozenset[Modality]
    supports_tool_calls: bool
    supports_streaming: bool
    notes: str

    def supports(self, modality: Modality) -> bool:
        return modality in self.input_modalities

    @property
    def input_modalities_values(self) -> list[str]:
        return sorted(item.value for item in self.input_modalities)

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "site": self.site,
            "model": self.model,
            "input_modalities": sorted(item.value for item in self.input_modalities),
            "output_modalities": sorted(item.value for item in self.output_modalities),
            "supports_tool_calls": self.supports_tool_calls,
            "supports_streaming": self.supports_streaming,
            # The backing model is a web chat, so there is no tokenizer to
            # count with. Usage is estimated and biased to over-count.
            "usage_is_estimated": True,
            "notes": self.notes,
        }


def resolve_capability(*, protocol: str, site: str, model: str) -> ResolvedCapability:
    """Intersect protocol, site and adapter capability for one model alias."""
    protocol_modalities = PROTOCOL_MODALITIES.get(protocol)
    if protocol_modalities is None:
        raise ValueError(f"unknown gateway protocol: {protocol}")
    adapter = ADAPTER_CAPABILITIES.get(site)
    if adapter is None:
        raise ValueError(f"unknown gateway site: {site}")
    return ResolvedCapability(
        protocol=protocol,
        site=site,
        model=model,
        input_modalities=frozenset(protocol_modalities & adapter.input_modalities),
        output_modalities=frozenset(adapter.output_modalities),
        supports_tool_calls=adapter.supports_tool_calls,
        supports_streaming=adapter.supports_streaming,
        notes=adapter.notes,
    )


# -- modality detection ------------------------------------------------------

#: Keys that identify non-text content in each protocol's content-part objects.
_PART_MODALITIES: dict[str, Modality] = {
    # OpenAI Responses
    "input_image": Modality.IMAGE,
    "input_file": Modality.FILE,
    "input_audio": Modality.AUDIO,
    "output_image": Modality.IMAGE,
    "image_url": Modality.IMAGE,
    "file_id": Modality.FILE,
    # Anthropic Messages
    "image": Modality.IMAGE,
    "document": Modality.FILE,
    # Gemini
    "inlineData": Modality.IMAGE,
    "inline_data": Modality.IMAGE,
    "fileData": Modality.FILE,
    "file_data": Modality.FILE,
}

#: Gemini carries the real type in a mime string rather than the key name.
_MIME_MODALITIES: tuple[tuple[str, Modality], ...] = (
    ("image/", Modality.IMAGE),
    ("audio/", Modality.AUDIO),
    ("video/", Modality.VIDEO),
)


def _mime_modality(value: Any, default: Modality) -> Modality:
    if isinstance(value, dict):
        mime = value.get("mimeType") or value.get("mime_type") or value.get("media_type") or ""
        if isinstance(mime, str):
            for prefix, modality in _MIME_MODALITIES:
                if mime.startswith(prefix):
                    return modality
            if mime:
                return Modality.FILE
    return default


def detect_modalities(value: Any) -> set[Modality]:
    """Walk an arbitrary protocol content structure and report what it carries.

    Detection is structural and protocol-agnostic on purpose: a new content
    shape shows up as an unsupported modality rather than being silently
    ignored by a normalizer that only knows how to read text.
    """
    found: set[Modality] = set()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            if isinstance(node, str):
                found.add(Modality.TEXT)
            return
        kind = node.get("type")
        if isinstance(kind, str) and kind in _PART_MODALITIES:
            base = _PART_MODALITIES[kind]
            found.add(_mime_modality(node.get("source") or node.get(kind) or node, base))
        for key, modality in _PART_MODALITIES.items():
            if key in node and key not in {"type"}:
                payload = node[key]
                if payload is not None:
                    found.add(_mime_modality(payload if isinstance(payload, dict) else node, modality))
        if isinstance(node.get("text"), str):
            found.add(Modality.TEXT)
        for key in ("content", "parts", "input", "messages", "contents"):
            if key in node:
                walk(node[key])

    walk(value)
    return found


def enforce_modalities(
    value: Any,
    capability: ResolvedCapability,
    *,
    where: str,
) -> set[Modality]:
    """Reject any modality the resolved route cannot deliver.

    Returns the detected modalities so the caller can record them.
    """
    detected = detect_modalities(value)
    for modality in sorted(detected, key=lambda item: item.value):
        if modality is Modality.TEXT:
            continue
        if not capability.supports(modality):
            raise UnsupportedModalityError(
                modality,
                protocol=capability.protocol,
                site=capability.site,
                model=capability.model,
                where=where,
            )
    return detected


def capability_report(models: Iterable[str], resolve_site: Any) -> list[dict[str, Any]]:
    """Build the capability table advertised by /v1/models and /health."""
    report: list[dict[str, Any]] = []
    for model in models:
        try:
            site = resolve_site(model)
        except ValueError:
            continue
        for protocol in sorted(PROTOCOL_MODALITIES):
            report.append(resolve_capability(protocol=protocol, site=site, model=model).as_dict())
    return report
