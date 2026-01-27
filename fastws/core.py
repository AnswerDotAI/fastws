"""Fast workspace tools for multi-repo management."""

from __future__ import annotations

__all__ = ["ws_clone", "ws_clone_cli", "ws_pull", "ws_pull_cli",
           "ws_status", "ws_status_cli", "ws_branches", "ws_branches_cli"]

import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastcore.script import call_parse
from fastgit import Git

def _load_repos(repos_file: str = "repos.txt") -> list[str]:
    p = Path(repos_file)
    if not p.exists(): raise SystemExit(f"File not found: {repos_file}")
    return [line.strip() for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")]

def _repo_dir(repo: str) -> str:
    return repo.split("/")[-1]

def _clone_one(repo: str) -> str:
    d = _repo_dir(repo)
    if Path(d).exists(): return f"✓ {d}: already exists"
    try:
        subprocess.run(["git", "clone", f"git@github.com:{repo}.git"], check=True, capture_output=True)
        return f"✓ {d}: cloned"
    except subprocess.CalledProcessError as e:
        return f"✗ {d}: {e.stderr.decode().strip()}"

def _pull_one(repo: str) -> str:
    d = _repo_dir(repo)
    if not Path(d).exists(): return f"✗ {d}: directory not found"
    try:
        res = subprocess.run(["git", "-C", d, "pull", "-q", "--stat"], check=True, capture_output=True, text=True)
        return f"✓ {d}" + (f"\n{res.stdout.strip()}" if res.stdout.strip() else "")
    except subprocess.CalledProcessError as e:
        return f"✗ {d}: {e.stderr.strip()}"

def ws_clone(
    repos_file: str = "repos.txt",  # File containing repo list (one per line: owner/repo)
    workers: int = 16,  # Number of parallel workers
):
    "Clone all repos from a repos file."
    repos = _load_repos(repos_file)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for result in as_completed([ex.submit(_clone_one, r) for r in repos]):
            print(result.result())

@call_parse
def ws_clone_cli(
    repos_file: str = "repos.txt",  # File containing repo list (one per line: owner/repo)
    workers: int = 16,  # Number of parallel workers
): ws_clone(repos_file, workers)


def ws_pull(
    repos_file: str = "repos.txt",  # File containing repo list
    workers: int = 16,  # Number of parallel workers
):
    "Pull updates for all repos."
    repos = _load_repos(repos_file)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for result in as_completed([ex.submit(_pull_one, r) for r in repos]):
            print(result.result())

@call_parse
def ws_pull_cli(
    repos_file: str = "repos.txt",  # File containing repo list
    workers: int = 16,  # Number of parallel workers
): ws_pull(repos_file, workers)


def ws_status(
    repos_file: str = "repos.txt",  # File containing repo list
):
    "Show uncommitted changes and unpushed commits across repos."
    repos = _load_repos(repos_file)
    for repo in repos:
        d = _repo_dir(repo)
        if not Path(d).exists(): continue
        g = Git(d)
        if not g.exists: continue
        changes = g.status('-s') or ""
        if isinstance(changes, list): changes = "\n".join(changes)
        unpushed = ""
        try: unpushed = g.log('--branches', '--not', '--remotes', format='%h %s') or ""
        except Exception: pass
        if isinstance(unpushed, list): unpushed = "\n".join(unpushed)
        if changes or unpushed:
            print(f"\n=== {d} ===")
            if changes: print(changes)
            if unpushed: print(unpushed)

@call_parse
def ws_status_cli(
    repos_file: str = "repos.txt",  # File containing repo list
): ws_status(repos_file)


def ws_branches(
    repos_file: str = "repos.txt",  # File containing repo list
    expected: str = "main",  # Expected branch name
):
    "Check if all repos are on the expected branch."
    repos = _load_repos(repos_file)
    for repo in repos:
        d = _repo_dir(repo)
        if not Path(d).exists():
            print(f"⚠️  {d}: directory not found")
            continue
        g = Git(d)
        if not g.exists:
            print(f"⚠️  {d}: not a git repo")
            continue
        branch = g.branch(show_current=True).strip()
        if branch == expected:
            print(f"✓ {d}: OK (on {expected})")
        else:
            print(f"⚠️  {d}: WARNING (on {branch})")

@call_parse
def ws_branches_cli(
    repos_file: str = "repos.txt",  # File containing repo list
    expected: str = "main",  # Expected branch name
): ws_branches(repos_file, expected)
