from __future__ import annotations

from .models import Capability, Freshness

CAPABILITIES: dict[str, Capability] = {
    "repo.read": Capability(name="repo.read", category="local", description="Read selected repository files."),
    "repo.search": Capability(name="repo.search", category="local", description="Search repository text for terms and symbols."),
    "repo.glob": Capability(name="repo.glob", category="local", description="Resolve file patterns inside the allowed repository root."),
    "git.diff": Capability(name="git.diff", category="local", description="Inspect the working tree diff without arbitrary shell execution."),
    "git.revision": Capability(name="git.revision", category="local", description="Capture the current repository revision."),
    "web.search": Capability(name="web.search", category="online", description="Search the public web for current or authoritative evidence."),
    "web.open": Capability(name="web.open", category="online", description="Open and read a discovered web source."),
    "docs.official": Capability(name="docs.official", category="online", description="Prefer official project/vendor documentation."),
    "source.upstream": Capability(name="source.upstream", category="online", description="Inspect upstream source when documentation is ambiguous."),
    "standards.search": Capability(name="standards.search", category="online", description="Locate applicable standards or normative references."),
    "community.search": Capability(name="community.search", category="online", description="Use community reports only as secondary experiential evidence."),
}

LOCAL_BASE = {"repo.read", "repo.search", "repo.glob", "git.revision"}
ONLINE_BASE = {"web.search", "web.open", "docs.official"}


def capability_names() -> list[str]:
    return sorted(name for name, item in CAPABILITIES.items() if item.enabled)


def resolve_capabilities(domain_capabilities: list[str], freshness: Freshness, include_git_diff: bool) -> list[str]:
    names = set(LOCAL_BASE)
    names.update(domain_capabilities)
    if freshness != Freshness.STATIC:
        names.update(ONLINE_BASE)
    if include_git_diff:
        names.add("git.diff")
    return sorted(name for name in names if name in CAPABILITIES and CAPABILITIES[name].enabled)
