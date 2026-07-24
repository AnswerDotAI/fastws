import fastws.core as core


def test_update_repos_file_appends_missing_entries(tmp_path):
    repos_path = tmp_path/"repos.txt"
    repos_path.write_text("AnswerDotAI/existing\n")

    added = core._update_repos_file(repos_path, ["AnswerDotAI/existing", "fastai/fastai"])

    assert added == ["fastai/fastai"]
    assert repos_path.read_text() == "AnswerDotAI/existing\nfastai/fastai\n"


def test_update_repos_file_is_case_insensitive(tmp_path):
    repos_path = tmp_path/"repos.txt"
    repos_path.write_text("AnswerDotAI/fastws\n")

    added = core._update_repos_file(repos_path, ["answerdotai/fastws"])

    assert added == []
    assert repos_path.read_text() == "AnswerDotAI/fastws\n"


def test_sync_workspace_pyproject_copies_template_and_adds_projects(tmp_path):
    (tmp_path/"pyproject.tmpl").write_text('[project]\nname = "uvws"\ndependencies = [\n    "ipython>=8.34.0",\n]\n\n[tool.uv.sources]\n\n')
    alpha = tmp_path/"alpha"
    beta = tmp_path/"beta"
    alpha.mkdir()
    beta.mkdir()
    (alpha/"pyproject.toml").write_text('[project]\nname = "alpha"\n')
    (beta/"pyproject.toml").write_text('[project]\nname = "beta"\n')

    added = core._sync_ws_pyproject(tmp_path/"pyproject.toml", tmp_path/"pyproject.tmpl", ["alpha", "beta"])
    content = (tmp_path/"pyproject.toml").read_text()

    assert added == ["alpha", "beta"]
    assert 'alpha = { workspace = true }' in content
    assert 'beta = { workspace = true }' in content
    assert '"alpha"' in content
    assert '"beta"' in content


def test_sync_workspace_pyproject_skips_case_only_source_differences(tmp_path):
    pyproject = tmp_path/"pyproject.toml"
    pyproject.write_text('[project]\nname = "uvws"\ndependencies = ["FastWS"]\n\n[tool.uv.sources]\nFastWS = { workspace = true }\n')

    added = core._sync_ws_pyproject(pyproject, tmp_path/"pyproject.tmpl", ["fastws"])

    assert added == []
    assert pyproject.read_text() == '[project]\nname = "uvws"\ndependencies = ["FastWS"]\n\n[tool.uv.sources]\nFastWS = { workspace = true }\n'


def test_workspace_projects_skip_excluded_dirs_and_template_names(tmp_path):
    (tmp_path/"pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["./*"]\nexclude = ["skip-*"]\n')
    keep = tmp_path/"keep"
    skip = tmp_path/"skip-template"
    templ = tmp_path/"template"
    keep.mkdir()
    skip.mkdir()
    templ.mkdir()
    (keep/"pyproject.toml").write_text('[project]\nname = "keepme"\n')
    (skip/"pyproject.toml").write_text('[project]\nname = "skipme"\n')
    (templ/"pyproject.toml").write_text('[project]\nname = "{repo}"\n')

    assert core._ws_projects(tmp_path) == ["keepme"]


# def test_write_pyright_pth_files_from_editable_finder(tmp_path):
#     site = tmp_path/".venv"/"lib"/"python3.12"/"site-packages"
#     site.mkdir(parents=True)
#     mapping = "MAPPING: dict[str, str] = {'demo': '/tmp/workspace/src/demo/__init__.py', 'tool': '/tmp/workspace/tool.py'}\n"
#     (site/"__editable___demo_finder.py").write_text(mapping)
#
#     created = core._write_pyright_pth_files(tmp_path)
#
#     assert [p.name for p in created] == ["_pyright_editable_demo.pth", "_pyright_editable_tool.pth"]
#     assert (site/"_pyright_editable_demo.pth").read_text() == "/tmp/workspace/src/demo\n"
#     assert (site/"_pyright_editable_tool.pth").read_text() == "/tmp/workspace\n"


def test_ws_sync_updates_workspace_and_runs_uv(tmp_path, monkeypatch):
    (tmp_path/"repos.txt").write_text("AnswerDotAI/existing\n")
    (tmp_path/"pyproject.tmpl").write_text('[project]\nname = "uvws"\ndependencies = [\n]\n\n[tool.uv.sources]\n\n')
    pkg = tmp_path/"newpkg"
    repo = tmp_path/"repo1"
    pkg.mkdir()
    repo.mkdir()
    (pkg/"pyproject.toml").write_text('[project]\nname = "newpkg"\n')
    (repo/".git").write_text("gitdir: .git/worktrees/repo1\n")
    (repo/"pyproject.toml").write_text('[project]\nname = "repo1pkg"\n')
    site = tmp_path/".venv"/"lib"/"python3.12"/"site-packages"
    site.mkdir(parents=True)
    (site/"__editable___newpkg_finder.py").write_text("MAPPING: dict[str, str] = {'newpkg': '/tmp/ws/src/newpkg/__init__.py'}\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:5] == ["git", "-C", str(repo), "remote", "get-url"]:
            class Res: stdout = "git@github.com:AnswerDotAI/repo1.git\n"
            return Res()
        if cmd[:4] == ["git", "-C", str(repo), "pull"]:
            class Res: stdout = ""
            return Res()
        if cmd == ["uv", "sync", "-U"]:
            class Res: stdout = ""
            return Res()
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    core.ws_sync(workspace=str(tmp_path))

    assert "AnswerDotAI/repo1" in (tmp_path/"repos.txt").read_text()
    pyproject = (tmp_path/"pyproject.toml").read_text()
    assert 'newpkg = { workspace = true }' in pyproject
    assert '"newpkg"' in pyproject
    assert next(i for i,(cmd,_) in enumerate(calls) if cmd[:4] == ["git", "-C", str(repo), "pull"]) < next(i for i,(cmd,_) in enumerate(calls) if cmd == ["uv", "sync", "-U"])
    assert any(cmd == ["uv", "sync", "-U"] and kwargs["cwd"] == tmp_path for cmd,kwargs in calls)


def test_ws_sync_uses_active_venv_parent_by_default(tmp_path, monkeypatch):
    workspace = tmp_path/"workspace"
    elsewhere = tmp_path/"elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    (workspace/"repos.txt").write_text("AnswerDotAI/existing\n")
    (workspace/"pyproject.tmpl").write_text('[project]\nname = "uvws"\ndependencies = [\n]\n\n[tool.uv.sources]\n\n')
    pkg = workspace/"newpkg"
    repo = workspace/"repo1"
    pkg.mkdir()
    repo.mkdir()
    (pkg/"pyproject.toml").write_text('[project]\nname = "newpkg"\n')
    (repo/".git").write_text("gitdir: .git/worktrees/repo1\n")
    (repo/"pyproject.toml").write_text('[project]\nname = "repo1pkg"\n')
    site = workspace/".venv"/"lib"/"python3.12"/"site-packages"
    site.mkdir(parents=True)
    (site/"__editable___newpkg_finder.py").write_text("MAPPING: dict[str, str] = {'newpkg': '/tmp/ws/src/newpkg/__init__.py'}\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:5] == ["git", "-C", str(repo), "remote", "get-url"]:
            class Res: stdout = "git@github.com:AnswerDotAI/repo1.git\n"
            return Res()
        if cmd[:4] == ["git", "-C", str(repo), "pull"]:
            class Res: stdout = ""
            return Res()
        if cmd == ["uv", "sync", "-U"]:
            class Res: stdout = ""
            return Res()
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    monkeypatch.setenv("VIRTUAL_ENV", str(workspace/".venv"))
    monkeypatch.chdir(elsewhere)

    core.ws_sync()

    assert "AnswerDotAI/repo1" in (workspace/"repos.txt").read_text()
    assert any(cmd == ["uv", "sync", "-U"] and kwargs["cwd"] == workspace for cmd,kwargs in calls)


def test_ws_add_clones_and_syncs(tmp_path, monkeypatch):
    (tmp_path/"repos.txt").write_text("AnswerDotAI/existing\n")
    sync_calls, clone_calls = [], []

    def fake_sync(*a): sync_calls.append(a)
    def fake_clone(repo, root="."):
        clone_calls.append((repo, root))
        return "✓ cloned"

    monkeypatch.setattr(core, "ws_sync", fake_sync)
    monkeypatch.setattr(core, "_clone_one", fake_clone)

    core.ws_add("answerdotai/fastws", workspace=str(tmp_path))

    assert (tmp_path/"repos.txt").read_text() == "AnswerDotAI/existing\nanswerdotai/fastws\n"
    assert clone_calls == [("answerdotai/fastws", str(tmp_path))]
    assert sync_calls == [(str(tmp_path), "repos.txt", "pyproject.toml", "pyproject.tmpl")]


def test_remove_from_repos_file_drops_matching_case_insensitive(tmp_path):
    repos_path = tmp_path/"repos.txt"
    repos_path.write_text("# header\nAnswerDotAI/keep\nAnswerDotAI/fastws\n")

    removed = core._remove_from_repos_file(repos_path, "answerdotai/fastws")

    assert removed is True
    assert repos_path.read_text() == "# header\nAnswerDotAI/keep\n"


def test_remove_from_repos_file_returns_false_when_absent(tmp_path):
    repos_path = tmp_path/"repos.txt"
    repos_path.write_text("AnswerDotAI/keep\n")

    removed = core._remove_from_repos_file(repos_path, "AnswerDotAI/gone")

    assert removed is False
    assert repos_path.read_text() == "AnswerDotAI/keep\n"


def test_remove_from_pyproject_drops_source_and_dep(tmp_path):
    pyproject = tmp_path/"pyproject.toml"
    pyproject.write_text('[project]\nname = "uvws"\ndependencies = [\n    "alpha",\n    "beta",\n]\n\n[tool.uv.sources]\nalpha = { workspace = true }\nbeta = { workspace = true }\n')

    removed = core._remove_from_pyproject(pyproject, ["Alpha"])
    content = pyproject.read_text()

    assert removed == ["alpha"]
    assert "alpha" not in content
    assert 'beta = { workspace = true }' in content
    assert '"beta"' in content


def test_remove_from_pyproject_returns_empty_when_absent(tmp_path):
    pyproject = tmp_path/"pyproject.toml"
    original = '[project]\nname = "uvws"\ndependencies = [\n    "beta",\n]\n\n[tool.uv.sources]\nbeta = { workspace = true }\n'
    pyproject.write_text(original)

    removed = core._remove_from_pyproject(pyproject, ["alpha"])

    assert removed == []
    assert pyproject.read_text() == original


def _setup_remove_ws(tmp_path):
    (tmp_path/"repos.txt").write_text("AnswerDotAI/keep\nAnswerDotAI/repo1\n")
    (tmp_path/"pyproject.toml").write_text('[project]\nname = "uvws"\ndependencies = [\n    "repo1pkg",\n    "keeppkg",\n]\n\n[tool.uv.sources]\nrepo1pkg = { workspace = true }\nkeeppkg = { workspace = true }\n')
    repo = tmp_path/"repo1"
    repo.mkdir()
    (repo/".git").write_text("gitdir: whatever\n")
    (repo/"pyproject.toml").write_text('[project]\nname = "repo1pkg"\n')
    return repo


def _fake_git_run(repo, status="", unpushed="", origin_rc=0):
    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["git", "-C", str(repo), "remote"]:
            class Res: returncode = origin_rc; stdout = "git@github.com:AnswerDotAI/repo1.git\n"
            return Res()
        if cmd[:4] == ["git", "-C", str(repo), "status"]:
            class Res: returncode = 0; stdout = status
            return Res()
        if cmd[:4] == ["git", "-C", str(repo), "log"]:
            class Res: returncode = 0; stdout = unpushed
            return Res()
        if cmd == ["uv", "sync"]:
            class Res: returncode = 0; stdout = ""
            return Res()
        raise AssertionError(f"Unexpected command: {cmd}")
    return fake_run


def test_ws_remove_happy_path(tmp_path, monkeypatch):
    repo = _setup_remove_ws(tmp_path)
    calls = []
    fake = _fake_git_run(repo)
    def tracking(cmd, **kwargs):
        calls.append(cmd)
        return fake(cmd, **kwargs)
    monkeypatch.setattr(core.subprocess, "run", tracking)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    core.ws_remove("AnswerDotAI/repo1", workspace=str(tmp_path))

    assert (tmp_path/"repos.txt").read_text() == "AnswerDotAI/keep\n"
    assert not repo.exists()
    content = (tmp_path/"pyproject.toml").read_text()
    assert "repo1pkg" not in content
    assert 'keeppkg = { workspace = true }' in content
    assert ["uv", "sync"] in calls


def test_ws_remove_matches_folder_name(tmp_path, monkeypatch):
    repo = _setup_remove_ws(tmp_path)
    monkeypatch.setattr(core.subprocess, "run", _fake_git_run(repo))
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    core.ws_remove("repo1", workspace=str(tmp_path))

    assert (tmp_path/"repos.txt").read_text() == "AnswerDotAI/keep\n"
    assert not repo.exists()
    assert "repo1pkg" not in (tmp_path/"pyproject.toml").read_text()


def test_ws_remove_invalid_repo_without_folder_errors(tmp_path, monkeypatch):
    import pytest
    _setup_remove_ws(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    with pytest.raises(SystemExit):
        core.ws_remove("nosuchdir", workspace=str(tmp_path))


def test_ws_remove_refuses_when_dirty(tmp_path, monkeypatch):
    import pytest
    repo = _setup_remove_ws(tmp_path)
    monkeypatch.setattr(core.subprocess, "run", _fake_git_run(repo, status="M core.py\n"))
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    with pytest.raises(SystemExit):
        core.ws_remove("AnswerDotAI/repo1", workspace=str(tmp_path))

    assert repo.exists()
    assert "AnswerDotAI/repo1" in (tmp_path/"repos.txt").read_text()
    assert "repo1pkg" in (tmp_path/"pyproject.toml").read_text()


def test_ws_remove_refuses_when_unpushed(tmp_path, monkeypatch):
    import pytest
    repo = _setup_remove_ws(tmp_path)
    monkeypatch.setattr(core.subprocess, "run", _fake_git_run(repo, unpushed="abc123\n"))
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    with pytest.raises(SystemExit):
        core.ws_remove("AnswerDotAI/repo1", workspace=str(tmp_path))

    assert repo.exists()


def test_ws_remove_no_keeps_directory_but_removes_metadata(tmp_path, monkeypatch):
    repo = _setup_remove_ws(tmp_path)
    calls = []
    fake = _fake_git_run(repo)
    def tracking(cmd, **kwargs):
        calls.append(cmd)
        return fake(cmd, **kwargs)
    monkeypatch.setattr(core.subprocess, "run", tracking)
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    core.ws_remove("AnswerDotAI/repo1", workspace=str(tmp_path))

    assert repo.exists()
    assert (tmp_path/"repos.txt").read_text() == "AnswerDotAI/keep\n"
    assert "repo1pkg" not in (tmp_path/"pyproject.toml").read_text()
    assert ["uv", "sync"] in calls


def test_ws_status_summarizes_unpushed_commits_by_default(tmp_path, monkeypatch, capsys):
    (tmp_path/"repo1").mkdir()

    class FakeGit:
        exists = True
        def __init__(self, d): pass
        def branch(self, **kwargs): return "main"
        def status(self, *args): return ""
        def log(self, *args, **kwargs): return "abc first commit"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core, "_load_repos", lambda repos_file="repos.txt": ["AnswerDotAI/repo1"])
    monkeypatch.setattr(core, "Git", FakeGit)

    core.ws_status()
    out = capsys.readouterr().out

    assert "unpushed commits" in out
    assert "abc first commit" not in out


def test_ws_status_ignores_unpushed_commits_off_main_by_default(tmp_path, monkeypatch, capsys):
    (tmp_path/"repo1").mkdir()

    class FakeGit:
        exists = True
        def __init__(self, d): pass
        def branch(self, **kwargs): return "feature"
        def status(self, *args): return ""
        def log(self, *args, **kwargs): return "abc first commit"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core, "_load_repos", lambda repos_file="repos.txt": ["AnswerDotAI/repo1"])
    monkeypatch.setattr(core, "Git", FakeGit)

    core.ws_status()

    assert capsys.readouterr().out == ""


def test_ws_status_shows_unpushed_commits_with_branches_flag(tmp_path, monkeypatch, capsys):
    (tmp_path/"repo1").mkdir()

    class FakeGit:
        exists = True
        def __init__(self, d): pass
        def branch(self, **kwargs): return "feature"
        def status(self, *args): return ""
        def log(self, *args, **kwargs): return "abc first commit"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(core, "_load_repos", lambda repos_file="repos.txt": ["AnswerDotAI/repo1"])
    monkeypatch.setattr(core, "Git", FakeGit)

    core.ws_status(branches=True)

    assert "abc first commit" in capsys.readouterr().out


def test_ws_sync_warns_and_skips_uv_when_member_lacks_pyproject(tmp_path, monkeypatch, capsys):
    (tmp_path/"repos.txt").write_text("")
    (tmp_path/"pyproject.tmpl").write_text('[project]\nname = "uvws"\ndependencies = [\n]\n\n[tool.uv.sources]\n\n')
    (tmp_path/"emptyclone").mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Res: stdout = ""
        return Res()

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    core.ws_sync(workspace=str(tmp_path))

    assert not any(cmd[:2] == ["uv", "sync"] for cmd in calls)
    assert "emptyclone" in capsys.readouterr().out


def _setup_add_dir(tmp_path, git=True, pyproject=True):
    (tmp_path/"repos.txt").write_text("AnswerDotAI/existing\n")
    d = tmp_path/"newproj"
    d.mkdir()
    if git: (d/".git").mkdir()
    if pyproject: (d/"pyproject.toml").write_text('[project]\nname = "newproj"\n')
    return d


def test_ws_add_resolves_local_dir(tmp_path, monkeypatch):
    d = _setup_add_dir(tmp_path)
    sync_calls, clone_calls = [], []
    monkeypatch.setattr(core, "ws_sync", lambda *a: sync_calls.append(a))
    monkeypatch.setattr(core, "_clone_one", lambda *a, **k: clone_calls.append(a) or "cloned")

    def fake_run(cmd, **kwargs):
        assert cmd[:5] == ["git", "-C", str(d), "remote", "get-url"]
        class Res: returncode = 0; stdout = "git@github.com:AnswerDotAI/newproj.git\n"
        return Res()

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    core.ws_add("newproj", workspace=str(tmp_path))

    assert "AnswerDotAI/newproj" in (tmp_path/"repos.txt").read_text()
    assert clone_calls == []
    assert len(sync_calls) == 1


def test_ws_add_dir_form_errors_name_the_missing_piece(tmp_path, monkeypatch):
    import pytest
    d = _setup_add_dir(tmp_path, git=False, pyproject=False)
    with pytest.raises(SystemExit, match="git"):
        core.ws_add("newproj", workspace=str(tmp_path))

    (d/".git").mkdir()
    def no_origin(cmd, **kwargs):
        class Res: returncode = 1; stdout = ""
        return Res()
    monkeypatch.setattr(core.subprocess, "run", no_origin)
    with pytest.raises(SystemExit, match="origin"):
        core.ws_add("newproj", workspace=str(tmp_path))

    def with_origin(cmd, **kwargs):
        class Res: returncode = 0; stdout = "git@github.com:AnswerDotAI/newproj.git\n"
        return Res()
    monkeypatch.setattr(core.subprocess, "run", with_origin)
    with pytest.raises(SystemExit, match="pyproject"):
        core.ws_add("newproj", workspace=str(tmp_path))


def test_ws_sync_upgrades_at_most_daily(tmp_path, monkeypatch):
    import os, time
    (tmp_path/"repos.txt").write_text("")
    (tmp_path/"pyproject.tmpl").write_text('[project]\nname = "uvws"\ndependencies = [\n]\n\n[tool.uv.sources]\n\n')
    crate = tmp_path/"crate"
    crate.mkdir()
    (crate/"pyproject.toml").write_text('[project]\nname = "crate"\n')
    (crate/"Cargo.toml").write_text('[package]\nname = "crate"\n')
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Res: stdout = ""; stderr = ""; returncode = 0
        return Res()

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    core.ws_sync(workspace=str(tmp_path))    # first sync of the day: full float
    assert ["uv", "sync", "-U"] in calls and ["cargo", "update"] in calls

    calls.clear()
    core.ws_sync(workspace=str(tmp_path))    # stamp is fresh: plain sync
    assert ["uv", "sync"] in calls and ["cargo", "update"] not in calls

    old = time.time() - 90000
    os.utime(core._upgrade_stamp(tmp_path), (old, old))
    calls.clear()
    core.ws_sync(workspace=str(tmp_path))    # stamp >24h old: floats again
    assert ["uv", "sync", "-U"] in calls and ["cargo", "update"] in calls

    calls.clear()
    core.ws_sync(workspace=str(tmp_path), upgrade=True)    # force flag overrides a fresh stamp
    assert ["uv", "sync", "-U"] in calls and ["cargo", "update"] in calls
