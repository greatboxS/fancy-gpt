from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


GATEWAY_URL = "http://127.0.0.1:8787"
BEGIN = "# >>> fancy-gpt model gateway >>>"
END = "# <<< fancy-gpt model gateway <<<"
CLAUDE_PICKER_OPTIONS = [
    {
        "model": "fancy-chatgpt",
        "label": "FancyGPT · ChatGPT",
        "description": "ChatGPT through the local FancyGPT gateway (launch with fancy-claude)",
    },
    {
        "model": "fancy-gemini",
        "label": "FancyGPT · Gemini",
        "description": "Gemini through the local FancyGPT gateway (launch with fancy-claude)",
    },
    {
        "model": "fancy-claude",
        "label": "FancyGPT · Claude",
        "description": "Claude-compatible alias through the local FancyGPT gateway (launch with fancy-claude)",
    },
]


@dataclass(frozen=True)
class GatewayClientConfig:
    client: str
    path: str
    changed: bool


def _replace_managed(text: str, block: str) -> str:
    if BEGIN in text and END in text:
        # Use the outermost markers so a malformed/partially migrated older
        # install containing duplicate BEGIN markers is repaired in one pass.
        before = text[: text.index(BEGIN)]
        after = text[text.rindex(END) + len(END) :]
        return before.rstrip() + "\n\n" + block + after
    if BEGIN in text:
        return text[: text.index(BEGIN)].rstrip() + "\n\n" + block + "\n"
    return text.rstrip() + ("\n\n" if text.strip() else "") + block + "\n"


def _remove_managed(text: str) -> str:
    if BEGIN not in text:
        return text
    before = text[: text.index(BEGIN)].rstrip()
    after = text[text.rindex(END) + len(END) :] if END in text else ""
    cleaned = before + after
    return cleaned.strip() + ("\n" if cleaned.strip() else "")


def _write_if_changed(path: Path, text: str) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if old == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def configure_codex(home: Path, base_url: str = GATEWAY_URL) -> GatewayClientConfig:
    path = home / ".codex" / "config.toml"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    # Codex 0.134+ rejects both legacy profile selectors and profile tables.
    # Remove only FancyGPT-owned names; unrelated user profiles stay untouched.
    old = re.sub(
        r'(?m)^\s*profile\s*=\s*["\']fancy-(?:chatgpt|gemini|claude)["\']\s*\n?', "", old
    )
    old = re.sub(
        r'(?ms)^\[profiles\.fancy-(?:chatgpt|gemini|claude)\]\s*\n.*?(?=^\[|^# <<< fancy-gpt|\Z)', "", old
    )
    block = f'''{BEGIN}
[model_providers.fancy-local]
name = "FancyGPT Local"
base_url = "{base_url}/v1"
wire_api = "responses"
{END}'''
    changed = _write_if_changed(path, _replace_managed(old, block))
    for model in ("fancy-chatgpt", "fancy-gemini", "fancy-claude"):
        profile_path = home / ".codex" / f"{model}.config.toml"
        profile = f'model = "{model}"\nmodel_provider = "fancy-local"\n'
        changed = _write_if_changed(profile_path, profile) or changed
    return GatewayClientConfig("codex", str(path), changed)


def configure_claude(home: Path, base_url: str = GATEWAY_URL) -> GatewayClientConfig:
    global_path = home / ".claude" / "settings.json"
    changed = False
    if global_path.exists() and global_path.read_text(encoding="utf-8").strip():
        global_data = json.loads(global_path.read_text(encoding="utf-8"))
        if isinstance(global_data, dict):
            if str(global_data.get("model", "")).startswith("fancy-"):
                global_data.pop("model", None)
            env = global_data.get("env")
            if isinstance(env, dict):
                if env.get("ANTHROPIC_BASE_URL") == base_url:
                    env.pop("ANTHROPIC_BASE_URL")
                if env.get("ANTHROPIC_API_KEY") == "local":
                    env.pop("ANTHROPIC_API_KEY")
                if not env:
                    global_data.pop("env", None)
            picker = global_data.setdefault("modelPicker", {})
            if not isinstance(picker, dict):
                raise ValueError(f"expected 'modelPicker' to be a JSON object in {global_path}")
            existing = picker.get("options", [])
            if not isinstance(existing, list):
                raise ValueError(f"expected 'modelPicker.options' to be a JSON array in {global_path}")
            fancy_ids = {row["model"] for row in CLAUDE_PICKER_OPTIONS}
            picker["options"] = [
                row for row in existing
                if not isinstance(row, dict) or row.get("model") not in fancy_ids
            ] + CLAUDE_PICKER_OPTIONS
            picker["replaceBuiltInOptions"] = False
            changed = _write_if_changed(global_path, json.dumps(global_data, indent=2) + "\n")

    # The base URL applies to the whole Claude process. Isolate it so normal
    # `claude` keeps claude.ai auth and native models.
    path = home / ".claude" / "fancy-gpt-settings.json"
    data: dict = {}
    if path.exists() and path.read_text(encoding="utf-8").strip():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"expected a JSON object in {path}")
        data = loaded
    env = data.setdefault("env", {})
    if not isinstance(env, dict):
        raise ValueError(f"expected 'env' to be a JSON object in {path}")
    # A bearer token satisfies Claude Code's gateway credential requirement
    # without conflicting with an existing claude.ai login (API_KEY does).
    env.pop("ANTHROPIC_API_KEY", None)
    env.update({"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": "local"})
    data["model"] = "fancy-chatgpt"
    data["modelPicker"] = {"options": CLAUDE_PICKER_OPTIONS, "replaceBuiltInOptions": True}
    changed = _write_if_changed(path, json.dumps(data, indent=2) + "\n") or changed
    return GatewayClientConfig("claude", str(path), changed)


def configure_gemini(home: Path, base_url: str = GATEWAY_URL) -> GatewayClientConfig:
    global_path = home / ".gemini" / ".env"
    old = global_path.read_text(encoding="utf-8") if global_path.exists() else ""
    changed = _write_if_changed(global_path, _remove_managed(old)) if BEGIN in old else False
    path = home / ".gemini" / "fancy-gpt.env"
    block = f'''{BEGIN}
GOOGLE_GEMINI_BASE_URL={base_url}
GEMINI_API_KEY=local
{END}'''
    changed = _write_if_changed(path, block + "\n") or changed
    return GatewayClientConfig("gemini", str(path), changed)


def configure_gateway_clients(home: Path, base_url: str = GATEWAY_URL) -> list[GatewayClientConfig]:
    return [
        configure_codex(home, base_url),
        configure_claude(home, base_url),
        configure_gemini(home, base_url),
    ]


def configs_json(configs: list[GatewayClientConfig]) -> str:
    return json.dumps([asdict(item) for item in configs], indent=2)
