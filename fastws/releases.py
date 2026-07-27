"Check which workspace repos have commits since their newest release."

__all__ = ["DEFAULT_SKIP", "check_release", "check_releases", "ws_releases"]

import re
from pathlib import Path

from fastcore.script import call_parse
from fastcore.parallel import parallel_async
from ghapi.core import APIError, GhApi
from packaging.version import InvalidVersion, Version

from .core import _dep_key, _load_repos, _resolve_path, _ws_root

try: import tomllib
except ModuleNotFoundError: import tomli as tomllib

# Start-of-message regexes for commits that need no release (curation noise, CI config, docs regen)
DEFAULT_SKIP = ["bump$", "Bump version to ", "meta", "auto", "README", "regen readme", "nbdev regen",
    "update .gitignore", r"\.?gitignore$", "workflow", "chkstyle skip", "tests$", "md$", "docs?$", "clean$", "CI$", "ignore$", "allowed_metadata_keys$"]

def _fastws_cfg(root: Path) -> dict:
    "The `[tool.fastws]` table from the workspace root pyproject.toml (empty when absent)."
    pyproj = root/"pyproject.toml"
    if not pyproj.exists(): return {}
    return tomllib.loads(pyproj.read_text(encoding="utf-8")).get("tool", {}).get("fastws", {})

def _skip_pats(skip=None, root: Path|None = None) -> list[re.Pattern]:
    "Compiled skip patterns: defaults + `[tool.fastws].release_skip` from the workspace pyproject + `skip`."
    pats = list(DEFAULT_SKIP)
    pats += _fastws_cfg(root or _ws_root()).get("release_skip", [])
    pats += [skip] if isinstance(skip, str) else list(skip or [])
    return [re.compile(p) for p in pats]

def _newest_tag(rels) -> str|None:
    "Tag of the highest-versioned release (publish timestamps can be out of order)."
    def key(r):
        try: return Version(r.tag_name.lstrip("v"))
        except InvalidVersion: return Version("0")
    return max(rels, key=key).tag_name if rels else None

async def check_release(repo: str, skip=None) -> list[str]|None:
    "First lines of commits on main since `repo`'s newest release; None when it has no releases. `repo` is 'name' or 'owner/name' (default owner AnswerDotAI)."
    owner, _, name = repo.rpartition("/")
    api = GhApi(owner=owner or "AnswerDotAI", repo=name)
    tag = _newest_tag(await api.repos.list_releases(per_page=100))
    if tag is None: return None
    try: cmp = await api.repos.compare_commits(basehead=f"{tag}...main")
    except APIError:  # default branch isn't `main` (e.g. older repos on `master`)
        branch = (await api.repos.get()).default_branch
        cmp = await api.repos.compare_commits(basehead=f"{tag}...{branch}")
    pats = skip if skip and isinstance(skip[0], re.Pattern) else _skip_pats(skip)
    return [m for c in cmp.commits if not any(p.match(m := c.commit["message"].splitlines()[0]) for p in pats)]

class ReleaseReport(list):
    "Rows of (repo, pending): pending is a list of commit summaries, None (no releases), or an Exception."
    def __repr__(self):
        out = []
        for repo, pending in self:
            if isinstance(pending, Exception): out.append(f"{repo}: ERROR {pending}")
            elif pending: out.extend([f"{repo} ({len(pending)} unreleased):", *(f"  - {m}" for m in pending)])
        quiet = [repo for repo, p in self if p is None]
        clean = [repo for repo, p in self if p == []]
        if quiet: out.append(f"no releases: {' '.join(quiet)}")
        if clean: out.append(f"up to date: {' '.join(clean)}")
        return "\n".join(out) or "nothing to check"

def _member_graph(root: Path) -> dict:
    "{package name: (repo dir name, [dep package names])} for every workspace member with a pyproject.toml."
    res = {}
    for p in sorted(root.glob("*/pyproject.toml")):
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        proj = data.get("project", {})
        if not (name := proj.get("name")): continue
        res[name.casefold()] = (p.parent.name, [_dep_key(d) for d in proj.get("dependencies", [])])
    return res

def _closure(name: str, graph: dict) -> set[str]:
    "Repo dir names for `name` and its transitive workspace-local dependencies."
    seen, todo = set(), [name.casefold()]
    while todo:
        if (n := todo.pop()) in seen or n not in graph: continue
        seen.add(n)
        todo += graph[n][1]
    return {graph[n][0] for n in seen}

async def check_releases(project: str = None, skip=None, workspace: str = "", repos_file: str = "repos.txt") -> ReleaseReport:
    "Sweep every workspace repo (or `project`'s transitive dependency closure) for unreleased commits; `[tool.fastws].release_exclude` names repos to leave out (apps that deploy rather than release)."
    root = _ws_root(workspace)
    repos = _load_repos(_resolve_path(root, repos_file))
    excl = {e.casefold() for e in _fastws_cfg(root).get("release_exclude", [])}
    repos = [r for r in repos if r.split("/")[-1].casefold() not in excl]
    if project:
        dirs = _closure(project, _member_graph(root))
        repos = [r for r in repos if r.split("/")[-1] in dirs]
    pats = _skip_pats(skip, root)
    res = await parallel_async(check_release, repos, skip=pats, return_exceptions=True)
    return ReleaseReport(zip([r.split("/")[-1] for r in repos], res))

@call_parse
async def ws_releases(
    project: str = None,  # Limit to this package's transitive workspace dependencies (default: all repos)
    skip: str = None,  # Extra start-of-message regex for commits that need no release
    workspace: str = "",  # Workspace root (defaults to the active venv's parent, else cwd)
    repos_file: str = "repos.txt",  # File containing repo list
):
    "Report workspace repos with commits since their newest release."
    print(repr(await check_releases(project=project, skip=skip, workspace=workspace, repos_file=repos_file)))
