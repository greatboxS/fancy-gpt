from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .secret_policy import SecretPolicy


class PatchRejected(ValueError):
    """A proposed change was refused before anything was written."""


@dataclass(frozen=True)
class AppliedFile:
    path: str
    before_sha256: str | None
    after_sha256: str
    bytes_written: int
    created: bool


@dataclass
class PatchResult:
    applied: list[AppliedFile] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RepositoryPatcher:
    """Apply model-proposed file edits, or refuse without touching the tree.

    A browser model cannot write files, so it proposes and the runtime applies.
    Every edit is therefore treated as untrusted input and must survive three
    checks before a single byte is written:

    * the target path stays inside the repository root;
    * the file the model was reasoning about is still exactly the file on disk
      (`base_sha256`), so a stale or concurrently-changed file is never
      clobbered;
    * for a replacement, `old` occurs exactly once.

    That last check is not pedantry. Responses are scraped out of the ChatGPT
    DOM, where the UI's soft wrapping can inject a newline inside a JSON string
    value; the parser tolerates that for prose, but in source code it would
    silently corrupt indentation. An exact-match precondition turns a mangled
    transfer into a clean refusal instead of broken code.

    The whole patch is validated before anything is written, so a rejected edit
    cannot leave the working tree half-changed.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        max_file_bytes: int = 512_000,
        max_files: int = 50,
        secret_policy: SecretPolicy | None = None,
    ) -> None:
        self.repo_root = repo_root.expanduser().resolve()
        if not self.repo_root.is_dir():
            raise PatchRejected(f"repository root does not exist: {self.repo_root}")
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.secret_policy = secret_policy or SecretPolicy()

    def _resolve(self, path: str) -> Path:
        normalized = path.replace("\\", "/").strip()
        if not normalized:
            raise PatchRejected("edit path is empty")
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts:
            raise PatchRejected(f"edit path must be repository-relative: {path}")
        denied = self.secret_policy.path_denied(normalized)
        if denied:
            raise PatchRejected(f"edit path is secret-bearing and refused: {path} ({denied})")
        resolved = (self.repo_root / pure).resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError:
            raise PatchRejected(f"edit path escapes the repository root: {path}") from None
        if resolved.is_symlink() or (resolved.exists() and not resolved.is_file()):
            raise PatchRejected(f"edit target is not a regular file: {path}")
        return resolved

    def _next_content(self, edit, current: str | None) -> str:
        if edit.content is not None:
            return edit.content
        if edit.old is None:
            raise PatchRejected(f"edit for {edit.path} supplies neither content nor old/new")
        if current is None:
            raise PatchRejected(f"cannot replace text in a file that does not exist: {edit.path}")
        occurrences = current.count(edit.old)
        if occurrences == 0:
            raise PatchRejected(
                f"replacement text was not found in {edit.path}; "
                "the proposed edit does not match the file it was given"
            )
        if occurrences > 1:
            raise PatchRejected(
                f"replacement text occurs {occurrences} times in {edit.path}; it must be unique"
            )
        return current.replace(edit.old, edit.new or "", 1)

    def plan(self, edits) -> list[tuple[Path, str, str | None, str, bool]]:
        """Validate every edit and return what would be written. Writes nothing."""
        if not edits:
            raise PatchRejected("a code change must contain at least one edit")
        if len(edits) > self.max_files:
            raise PatchRejected(f"code change touches {len(edits)} files; limit is {self.max_files}")

        seen: set[str] = set()
        planned: list[tuple[Path, str, str | None, str, bool]] = []
        for edit in edits:
            if edit.path in seen:
                raise PatchRejected(f"the same file is edited twice in one change: {edit.path}")
            seen.add(edit.path)
            target = self._resolve(edit.path)
            exists = target.is_file()
            current = target.read_text(encoding="utf-8") if exists else None
            before = sha256_text(current) if current is not None else None

            if edit.base_sha256:
                if before is None:
                    raise PatchRejected(f"edit declares a base hash for a file that does not exist: {edit.path}")
                if edit.base_sha256 != before:
                    raise PatchRejected(
                        f"{edit.path} changed since it was supplied "
                        f"(expected {edit.base_sha256[:12]}, found {before[:12]})"
                    )
            elif exists and edit.content is not None:
                raise PatchRejected(
                    f"replacing the whole of an existing file requires base_sha256: {edit.path}"
                )

            nxt = self._next_content(edit, current)
            if nxt == current:
                raise PatchRejected(f"edit for {edit.path} would change nothing")
            encoded = nxt.encode("utf-8")
            if len(encoded) > self.max_file_bytes:
                raise PatchRejected(f"{edit.path} would exceed max_file_bytes ({len(encoded)})")
            planned.append((target, edit.path, before, nxt, not exists))
        return planned

    def apply(self, edits) -> PatchResult:
        planned = self.plan(edits)
        result = PatchResult()
        for target, rel, before, content, created in planned:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
            result.applied.append(AppliedFile(
                path=rel,
                before_sha256=before,
                after_sha256=sha256_text(content),
                bytes_written=len(content.encode("utf-8")),
                created=created,
            ))
        return result


@dataclass(frozen=True)
class CommandReceipt:
    """Proof that the runtime itself ran something, not that a model said so."""

    argv: list[str]
    exit_code: int
    duration_seconds: float
    output_sha256: str
    output_tail: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class CommandRunner:
    """Run a verification command from an allowlist and record what happened.

    The command never comes from the model as free text. A teammate may only
    name a check the operator already allowed, so a proposed patch can be
    verified without handing an online model a shell.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        allowed: dict[str, list[str]] | None = None,
        timeout_seconds: float = 900.0,
        output_tail_chars: int = 4000,
    ) -> None:
        self.repo_root = repo_root.expanduser().resolve()
        self.allowed = allowed or {}
        self.timeout_seconds = timeout_seconds
        self.output_tail_chars = output_tail_chars

    def run(self, check: str) -> CommandReceipt:
        argv = self.allowed.get(check)
        if argv is None:
            raise PatchRejected(
                f"unknown verification check: {check}; allowed: {sorted(self.allowed) or 'none'}"
            )
        import time

        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                argv,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            output = ((exc.stdout or "") if isinstance(exc.stdout, str) else "") + (
                (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            )
            exit_code = 124
        return CommandReceipt(
            argv=list(argv),
            exit_code=exit_code,
            duration_seconds=round(time.monotonic() - started, 3),
            output_sha256=sha256_text(output),
            output_tail=output[-self.output_tail_chars:],
            timed_out=timed_out,
        )
