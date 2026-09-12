from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import ContextRequirementError, ContextSecurityError, ContextTooLargeError
from .models import (
    ArtifactRole,
    ContextArtifact,
    ContextPack,
    Priority,
    RawRequest,
    RequestMode,
    ResearchManifest,
)
from .request_sanitizer import candidate_paths
from .secret_policy import SecretPolicy

_PRIORITY_RANK = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2, Priority.P3: 3, Priority.P4: 4}
_HARD_PRUNE_DIRS = {
    ".git", ".venv", ".fancy-gpt", ".pytest_cache", "__pycache__", "node_modules",
    "build", "dist", ".wheel-sim", ".wheel-test", ".mypy_cache", ".ruff_cache",
    "downloads", "sstate-cache",
}


@dataclass
class _AcquisitionBudget:
    max_files: int
    max_bytes: int
    deadline: float
    files: int = 0
    bytes: int = 0

    def consume(self, size: int) -> bool:
        if time.monotonic() > self.deadline:
            return False
        if self.files + 1 > self.max_files or self.bytes + size > self.max_bytes:
            return False
        self.files += 1
        self.bytes += size
        return True


class ContextBuilder:
    def __init__(
        self,
        max_file_bytes: int = 512_000,
        *,
        max_scan_files: int = 20_000,
        max_scan_bytes: int = 256 * 1024 * 1024,
        max_scan_seconds: float = 10.0,
        git_timeout_seconds: float = 5.0,
        git_output_bytes: int = 2 * 1024 * 1024,
        secret_policy: SecretPolicy | None = None,
    ) -> None:
        self.max_file_bytes = max_file_bytes
        self.max_scan_files = max_scan_files
        self.max_scan_bytes = max_scan_bytes
        self.max_scan_seconds = max_scan_seconds
        self.git_timeout_seconds = git_timeout_seconds
        self.git_output_bytes = git_output_bytes
        self.secret_policy = secret_policy or SecretPolicy()

    def build(self, request: RawRequest, manifest: ResearchManifest) -> ContextPack:
        root = Path(request.repo_root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ContextSecurityError(f"repo_root does not exist or is not a directory: {root}")

        excluded_candidate_paths = candidate_paths(request)
        candidates: dict[Path, Priority] = {}
        omitted: list[str] = []
        acquisition = _AcquisitionBudget(
            max_files=self.max_scan_files,
            max_bytes=self.max_scan_bytes,
            deadline=time.monotonic() + self.max_scan_seconds,
        )

        if request.mode != RequestMode.DESIGN:
            for pattern in request.include:
                self._collect_pattern(root, pattern, Priority.P1, request.exclude, candidates, excluded_candidate_paths)
        elif request.include:
            omitted.append("independence:request-include-suppressed")

        for req in manifest.local_context_requirements:
            for exact in req.exact_paths:
                self._collect_exact(root, exact, req.priority, candidates, excluded_candidate_paths)
            for pattern in req.patterns:
                self._collect_pattern(root, pattern, req.priority, request.exclude, candidates, excluded_candidate_paths)
            if req.search_terms:
                complete = self._search_terms(
                    root, req.search_terms, req.priority, request.exclude, candidates,
                    excluded_candidate_paths, acquisition,
                )
                if not complete:
                    omitted.append(f"acquisition-budget:{req.id}")
                    if req.required:
                        raise ContextRequirementError(
                            f"required local context search exceeded acquisition budget: {req.id}"
                        )

        artifacts: list[ContextArtifact] = []
        for spec in request.artifacts:
            if request.mode == RequestMode.DESIGN and spec.role == ArtifactRole.CANDIDATE_SOLUTION:
                omitted.append("independence:candidate-solution")
                continue
            if spec.content is not None:
                label = spec.name or "inline"
                secret = self.secret_policy.sanitize(label, spec.content)
                if not secret.allowed:
                    omitted.append(f"secret-policy:{label}:{secret.reason}")
                    continue
                text = secret.content
                if secret.redactions:
                    omitted.append(f"secret-redacted:{label}:{secret.redactions}")
                raw = text.encode("utf-8")
                artifacts.append(ContextArtifact(
                    id=self._artifact_id(label, raw),
                    source="inline",
                    path=label,
                    kind=spec.kind,
                    role=spec.role,
                    priority=spec.priority,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size=len(raw),
                    content=text,
                ))
            elif spec.path:
                self._collect_exact(root, spec.path, spec.priority, candidates, excluded_candidate_paths)

        # Git diff can easily contain the candidate solution. Independent design
        # therefore excludes it by policy even if the raw request asked for it.
        if request.include_git_diff and request.mode != RequestMode.DESIGN:
            diff = self._git_diff(root)
            if diff:
                secret = self.secret_policy.sanitize("<git-diff>", diff)
                diff = secret.content if secret.allowed else ""
                if secret.redactions:
                    omitted.append(f"secret-redacted:<git-diff>:{secret.redactions}")
            if diff:
                raw = diff.encode("utf-8")
                artifacts.append(ContextArtifact(
                    id=self._artifact_id("git-diff", raw),
                    source="git-diff",
                    path="<git-diff>",
                    kind="diff",
                    role=ArtifactRole.CONTEXT,
                    priority=Priority.P1,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size=len(raw),
                    content=diff,
                ))

        ordered = sorted(candidates.items(), key=lambda item: (_PRIORITY_RANK[item[1]], str(item[0])))
        budget = min(request.max_context_bytes, manifest.research_budget.max_context_bytes)
        total = sum(a.size for a in artifacts)
        if total > budget:
            raise ContextTooLargeError("inline/git-diff context exceeds context budget")

        for path, priority in ordered:
            try:
                raw = path.read_bytes()
            except OSError:
                omitted.append(f"unreadable:{self._relative(root, path)}")
                continue
            if len(raw) > self.max_file_bytes:
                if priority == Priority.P0:
                    raise ContextTooLargeError(f"required P0 file exceeds max_file_bytes: {path}")
                omitted.append(f"oversize:{self._relative(root, path)}")
                continue
            if b"\x00" in raw[:4096]:
                omitted.append(f"binary:{self._relative(root, path)}")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                omitted.append(f"non-utf8:{self._relative(root, path)}")
                continue
            rel = path.relative_to(root).as_posix()
            secret = self.secret_policy.sanitize(rel, text)
            if not secret.allowed:
                if priority == Priority.P0:
                    raise ContextRequirementError(f"required P0 context denied by secret policy: {rel}")
                omitted.append(f"secret-policy:{rel}:{secret.reason}")
                continue
            text = secret.content
            raw = text.encode("utf-8")
            if secret.redactions:
                omitted.append(f"secret-redacted:{rel}:{secret.redactions}")
            if total + len(raw) > budget:
                if priority == Priority.P0:
                    raise ContextTooLargeError("required P0 context does not fit context budget")
                omitted.append(f"budget:{self._relative(root, path)}")
                continue
            artifacts.append(ContextArtifact(
                id=self._artifact_id(rel, raw),
                source="filesystem",
                path=rel,
                kind=self._kind(path),
                role=ArtifactRole.CONTEXT,
                priority=priority,
                sha256=hashlib.sha256(raw).hexdigest(),
                size=len(raw),
                content=text,
            ))
            total += len(raw)

        satisfied = self._validate_required_context(manifest, artifacts)
        revision = self._git_revision(root)
        digest = hashlib.sha256()
        for item in sorted(artifacts, key=lambda x: (x.path, x.sha256)):
            digest.update(item.path.encode())
            digest.update(item.sha256.encode())
            digest.update(item.role.value.encode())
        digest.update((revision or "").encode())

        return ContextPack(
            repo_root="<local-repo-root>",
            artifacts=artifacts,
            total_bytes=total,
            context_hash=digest.hexdigest(),
            omitted=omitted,
            revision=revision,
            satisfied_requirements=satisfied,
        )

    @staticmethod
    def validate_allowed_root(repo_root: str, allowed_roots: list[Path]) -> Path:
        resolved = Path(repo_root).expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ContextSecurityError(f"repo_root does not exist or is not a directory: {resolved}")
        for allowed in allowed_roots:
            allowed_resolved = allowed.expanduser().resolve()
            try:
                resolved.relative_to(allowed_resolved)
                return resolved
            except ValueError:
                continue
        raise ContextSecurityError(f"repo_root is outside configured allowed roots: {resolved}")

    def _safe_path(self, root: Path, candidate: Path) -> Path:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ContextSecurityError(f"path escapes repository root: {candidate}") from exc
        return resolved

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)

    def _excluded(self, root: Path, path: Path, excludes: list[str]) -> bool:
        rel = path.relative_to(root).as_posix()
        parts = set(path.relative_to(root).parts)
        if parts & _HARD_PRUNE_DIRS or any(part.endswith(".egg-info") for part in parts):
            return True
        if any(part.startswith("build-") or part.startswith("tmp-") or part == "tmp" for part in parts):
            return True
        for pattern in excludes:
            variants = [pattern]
            if pattern.startswith("**/"):
                variants.append(pattern[3:])
            if any(fnmatch.fnmatch(rel, item) or fnmatch.fnmatch("/" + rel, item) for item in variants):
                return True
        return False

    @staticmethod
    def _normalized_rel(value: str) -> str:
        return value.replace("\\", "/").lstrip("./")

    def _is_candidate(self, rel: str, excluded_candidate_paths: set[str]) -> bool:
        norm = self._normalized_rel(rel)
        return any(norm == self._normalized_rel(item) for item in excluded_candidate_paths)

    def _collect_exact(
        self,
        root: Path,
        value: str,
        priority: Priority,
        candidates: dict[Path, Priority],
        excluded_candidate_paths: set[str],
    ) -> None:
        # A leading "/" is a common gitignore-style "repo-root anchored" path,
        # not a filesystem-absolute one; treat it as root-relative rather than
        # rejecting it outright. `..` traversal is still hard-rejected, and
        # `_safe_path` below independently re-enforces containment in `root`.
        normalized = value.lstrip("/")
        if not normalized or ".." in Path(normalized).parts:
            raise ContextSecurityError(f"unsafe exact path: {value}")
        if self._is_candidate(normalized, excluded_candidate_paths):
            return
        raw = root / normalized
        if raw.is_symlink():
            resolved = self._safe_path(root, raw)
            if not resolved.is_file():
                return
            path = resolved
        else:
            path = self._safe_path(root, raw)
        if path.is_file():
            self._add_candidate(path, priority, candidates)

    def _collect_pattern(
        self,
        root: Path,
        pattern: str,
        priority: Priority,
        excludes: list[str],
        candidates: dict[Path, Priority],
        excluded_candidate_paths: set[str],
    ) -> None:
        # Same root-anchored-vs-absolute distinction as _collect_exact.
        normalized = pattern.lstrip("/")
        if not normalized or ".." in Path(normalized).parts:
            raise ContextSecurityError(f"unsafe include pattern: {pattern}")
        for path in root.glob(normalized):
            if path.is_symlink():
                continue
            if path.is_file() and not self._excluded(root, path, excludes):
                rel = path.relative_to(root).as_posix()
                if self._is_candidate(rel, excluded_candidate_paths):
                    continue
                safe = self._safe_path(root, path)
                self._add_candidate(safe, priority, candidates)

    def _search_terms(
        self,
        root: Path,
        terms: list[str],
        priority: Priority,
        excludes: list[str],
        candidates: dict[Path, Priority],
        excluded_candidate_paths: set[str],
        acquisition: _AcquisitionBudget,
    ) -> bool:
        lowered = [term.lower() for term in terms if term]
        if not lowered:
            return True
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            kept: list[str] = []
            for name in dirnames:
                child = current / name
                if child.is_symlink() or name in _HARD_PRUNE_DIRS or name.endswith(".egg-info"):
                    continue
                if self._excluded(root, child, excludes):
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                path = current / name
                if path.is_symlink() or self._excluded(root, path, excludes):
                    continue
                rel = path.relative_to(root).as_posix()
                if self._is_candidate(rel, excluded_candidate_paths):
                    continue
                try:
                    safe = self._safe_path(root, path)
                    size = safe.stat().st_size
                    if size > self.max_file_bytes:
                        continue
                    if not acquisition.consume(size):
                        return False
                    text = safe.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeError):
                    continue
                low = text.lower()
                if any(term in low for term in lowered):
                    self._add_candidate(safe, priority, candidates)
        return True

    @staticmethod
    def _add_candidate(path: Path, priority: Priority, candidates: dict[Path, Priority]) -> None:
        prev = candidates.get(path)
        if prev is None or _PRIORITY_RANK[priority] < _PRIORITY_RANK[prev]:
            candidates[path] = priority

    @staticmethod
    def _artifact_id(label: str, raw: bytes) -> str:
        return hashlib.sha256(label.encode("utf-8") + b"\0" + raw).hexdigest()[:16]

    @staticmethod
    def _kind(path: Path) -> str:
        suffix = path.suffix.lower().lstrip(".")
        return suffix or "text"

    def _run_git(self, root: Path, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
                timeout=self.git_timeout_seconds,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        if result.returncode != 0:
            return ""
        raw = result.stdout.encode("utf-8", errors="replace")
        if len(raw) > self.git_output_bytes:
            return raw[: self.git_output_bytes].decode("utf-8", errors="ignore") + "\n<git-output-truncated>\n"
        return result.stdout

    def _git_revision(self, root: Path) -> str | None:
        value = self._run_git(root, ["rev-parse", "HEAD"]).strip()
        return value or None

    def _git_diff(self, root: Path) -> str:
        return self._run_git(root, ["diff", "--no-ext-diff", "--no-textconv", "--"])

    def _validate_required_context(self, manifest: ResearchManifest, artifacts: list[ContextArtifact]) -> list[str]:
        satisfied: list[str] = []
        paths = [item.path for item in artifacts]
        for req in manifest.local_context_requirements:
            matched = False
            for exact in req.exact_paths:
                if self._normalized_rel(exact) in [self._normalized_rel(p) for p in paths]:
                    matched = True
                    break
            if not matched:
                for pattern in req.patterns:
                    normalized_pattern = pattern.lstrip("/")
                    variants = [pattern, normalized_pattern]
                    if "**/" in normalized_pattern:
                        variants.append(normalized_pattern.replace("**/", ""))
                    if any(any(fnmatch.fnmatch(path, variant) for variant in variants) for path in paths):
                        matched = True
                        break
            if not matched and req.search_terms:
                lowered = [term.lower() for term in req.search_terms]
                if any(any(term in art.content.lower() for term in lowered) for art in artifacts):
                    matched = True
            if matched:
                satisfied.append(req.id)
            elif req.required:
                raise ContextRequirementError(
                    f"required local context requirement was not satisfied after security/independence/budget filtering: {req.id}"
                )
        return satisfied
