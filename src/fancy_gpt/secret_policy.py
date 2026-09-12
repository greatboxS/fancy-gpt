from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class SecretScanResult:
    allowed: bool
    content: str
    redactions: int = 0
    reason: str | None = None


class SecretPolicy:
    """Conservative outbound DLP for context sent to online models.

    The policy intentionally targets high-confidence secret material. It avoids
    broad password/token heuristics that would redact normal source code and
    configuration examples. Files that are intrinsically secret-bearing are
    denied; high-confidence credentials embedded in otherwise useful files are
    redacted in-place.
    """

    _DENIED_BASENAMES = {
        ".env",
        "credentials",
        "credentials.json",
        "service-account.json",
        "service_account.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
    _DENIED_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".keystore", ".jks"}
    _ALLOW_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}

    _PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "private-key",
            re.compile(
                r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
                re.DOTALL,
            ),
        ),
        ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
        ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
        ("openai-token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    )

    def path_denied(self, path: str) -> str | None:
        normalized = path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        name = pure.name.lower()
        if name in self._ALLOW_ENV_EXAMPLES:
            return None
        if name in self._DENIED_BASENAMES:
            return f"secret-bearing-file:{name}"
        if pure.suffix.lower() in self._DENIED_SUFFIXES:
            return f"secret-bearing-extension:{pure.suffix.lower()}"
        if ".ssh" in {part.lower() for part in pure.parts} and name.startswith("id_"):
            return "private-ssh-key"
        return None

    def sanitize(self, path: str, content: str) -> SecretScanResult:
        denied = self.path_denied(path)
        if denied:
            return SecretScanResult(allowed=False, content="", reason=denied)

        redactions = 0
        sanitized = content
        for label, pattern in self._PATTERNS:
            sanitized, count = pattern.subn(f"<redacted:{label}>", sanitized)
            redactions += count
        return SecretScanResult(allowed=True, content=sanitized, redactions=redactions)
