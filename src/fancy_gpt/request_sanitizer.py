from __future__ import annotations

from .models import ArtifactRole, RawRequest, RequestMode


def request_metadata_for_online(request: RawRequest) -> dict:
    """Return an online-safe request view.

    Artifact bodies never enter the planner. In independent-design mode,
    candidate-solution artifacts are removed entirely so even their names/paths
    cannot anchor the planner. The host repository path is redacted.
    """
    payload = request.model_dump(mode="json")
    payload["repo_root"] = "<local-repo-root>"
    artifacts: list[dict] = []
    for spec in request.artifacts:
        if request.mode == RequestMode.DESIGN and spec.role == ArtifactRole.CANDIDATE_SOLUTION:
            continue
        item = spec.model_dump(mode="json")
        item.pop("content", None)
        artifacts.append(item)
    payload["artifacts"] = artifacts
    if request.mode == RequestMode.DESIGN:
        # Independent design must not be anchored by candidate-oriented local
        # navigation metadata. Requirements remain represented by the explicit
        # objective/focus/questions and requirement-role artifacts, while
        # include/exclude/notes can reveal candidate filenames, module names,
        # or implementation hints even when candidate bodies are removed.
        payload["include"] = []
        payload["exclude"] = []
        payload["notes"] = None
        payload["include_git_diff"] = False
    return payload


def candidate_paths(request: RawRequest) -> set[str]:
    paths: set[str] = set()
    if request.mode != RequestMode.DESIGN:
        return paths
    for spec in request.artifacts:
        if spec.role == ArtifactRole.CANDIDATE_SOLUTION and spec.path:
            paths.add(spec.path.replace("\\", "/"))
    return paths
