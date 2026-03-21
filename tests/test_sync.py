from pathlib import Path

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


def test_write_pyright_pth_files_from_editable_finder(tmp_path):
    site = tmp_path/".venv"/"lib"/"python3.12"/"site-packages"
    site.mkdir(parents=True)
    (site/"__editable___demo_finder.py").write_text("MAPPING: dict[str, str] = {'demo': '/tmp/workspace/src/demo/__init__.py', 'tool': '/tmp/workspace/tool.py'}\n")

    created = core._write_pyright_pth_files(tmp_path)

    assert [p.name for p in created] == ["_pyright_editable_demo.pth", "_pyright_editable_tool.pth"]
    assert (site/"_pyright_editable_demo.pth").read_text() == "/tmp/workspace/src/demo\n"
    assert (site/"_pyright_editable_tool.pth").read_text() == "/tmp/workspace\n"


def test_ws_sync_updates_workspace_and_runs_uv(tmp_path, monkeypatch):
    (tmp_path/"repos.txt").write_text("AnswerDotAI/existing\n")
    (tmp_path/"pyproject.tmpl").write_text('[project]\nname = "uvws"\ndependencies = [\n]\n\n[tool.uv.sources]\n\n')
    pkg = tmp_path/"newpkg"
    repo = tmp_path/"repo1"
    pkg.mkdir()
    repo.mkdir()
    (pkg/"pyproject.toml").write_text('[project]\nname = "newpkg"\n')
    (repo/".git").write_text("gitdir: .git/worktrees/repo1\n")
    site = tmp_path/".venv"/"lib"/"python3.12"/"site-packages"
    site.mkdir(parents=True)
    (site/"__editable___newpkg_finder.py").write_text("MAPPING: dict[str, str] = {'newpkg': '/tmp/ws/src/newpkg/__init__.py'}\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:5] == ["git", "-C", str(repo), "remote", "get-url"]:
            class Res: stdout = "git@github.com:AnswerDotAI/repo1.git\n"
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
    assert (site/"_pyright_editable_newpkg.pth").read_text() == "/tmp/ws/src/newpkg\n"
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
    site = workspace/".venv"/"lib"/"python3.12"/"site-packages"
    site.mkdir(parents=True)
    (site/"__editable___newpkg_finder.py").write_text("MAPPING: dict[str, str] = {'newpkg': '/tmp/ws/src/newpkg/__init__.py'}\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:5] == ["git", "-C", str(repo), "remote", "get-url"]:
            class Res: stdout = "git@github.com:AnswerDotAI/repo1.git\n"
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


def test_ws_add_updates_repos_then_runs_sync(tmp_path, monkeypatch):
    (tmp_path/"repos.txt").write_text("AnswerDotAI/existing\n")
    calls = []

    def fake_sync(workspace, repos_file, pyproject_file, template_file):
        calls.append((workspace, repos_file, pyproject_file, template_file))

    monkeypatch.setattr(core, "ws_sync", fake_sync)

    core.ws_add("answerdotai/fastws", workspace=str(tmp_path))

    assert (tmp_path/"repos.txt").read_text() == "AnswerDotAI/existing\nanswerdotai/fastws\n"
    assert calls == [(str(tmp_path), "repos.txt", "pyproject.toml", "pyproject.tmpl")]


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
