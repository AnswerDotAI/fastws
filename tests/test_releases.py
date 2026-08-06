import asyncio, pytest

from fastcore.basics import AttrDict

import fastws.releases as relmod


def _rel(tag): return AttrDict(tag_name=tag)


def test_newest_tag_by_version_not_publish_order():
    assert relmod._newest_tag([_rel("v0.1.17"), _rel("v0.1.18"), _rel("v0.1.9")]) == "v0.1.18"
    assert relmod._newest_tag([]) is None


def test_skip_pats_defaults_and_config(tmp_path):
    (tmp_path/"pyproject.toml").write_text('[tool.fastws]\nrelease_skip = ["docs only"]\n', encoding="utf-8")
    pats = relmod._skip_pats("wip", root=tmp_path)
    hits = lambda m: any(p.match(m) for p in pats)
    assert hits("bump") and hits("Bump version to 0.2.2") and hits("update .gitignore")
    assert hits("docs only: fix typo")  # workspace config
    assert hits("wip checkpoint")       # per-call extra
    assert not hits("bumpy road ahead") # bump$ is anchored
    assert not hits("fixes #30")
    for m in (".gitignore", "gitignore", "docs", "doc", "clean", "CI", "meta", "ignore", "allowed_metadata_keys"): assert hits(m)
    for m in ("deps", "CI fixups", "document the API", "cleanup tests"): assert not hits(m)  # deps IS release-worthy; anchored bare words only


def test_release_report_repr():
    rep = relmod.ReleaseReport([("mdhtml", ["fixes #30"]), ("solveit", None), ("fastcore", []), ("bad", ValueError("boom"))])
    txt = repr(rep)
    assert "mdhtml (1 unreleased):" in txt and "  - fixes #30" in txt
    assert "no releases: solveit" in txt
    assert "up to date: fastcore" in txt
    assert "bad: ERROR boom" in txt


def test_release_exclude(tmp_path, monkeypatch):
    (tmp_path/"repos.txt").write_text("AnswerDotAI/libx\nAnswerDotAI/appy\n", encoding="utf-8")
    (tmp_path/"pyproject.toml").write_text('[tool.fastws]\nrelease_exclude = ["Appy"]\n', encoding="utf-8")
    checked = []
    async def fake(repo, skip=None): return checked.append(repo) or []
    monkeypatch.setattr(relmod, "check_release", fake)

    rep = asyncio.run(relmod.check_releases(workspace=str(tmp_path)))

    assert checked == ["AnswerDotAI/libx"]  # appy excluded, case-insensitively
    assert [r for r, _ in rep] == ["libx"]


def _member(tmp_path, dirname, pkg=None, deps=()):
    d = tmp_path/dirname
    d.mkdir()
    dep_s = ", ".join(f'"{x}"' for x in deps)
    (d/"pyproject.toml").write_text(f'[project]\nname = "{pkg or dirname}"\ndependencies = [{dep_s}]\n', encoding="utf-8")


def test_check_releases_scoping(tmp_path, monkeypatch):
    (tmp_path/"repos.txt").write_text("AnswerDotAI/libx\nAnswerDotAI/appy\nAnswerDotAI/exty\n", encoding="utf-8")
    _member(tmp_path, "libx", pkg="libx-cli")
    _member(tmp_path, "appy", deps=["libx-cli"])
    _member(tmp_path, "exty")
    checked = []
    async def fake(repo, skip=None): return checked.append(repo) or []
    monkeypatch.setattr(relmod, "check_release", fake)
    run = lambda **kw: asyncio.run(relmod.check_releases(workspace=str(tmp_path), **kw))

    monkeypatch.chdir(tmp_path)
    run()
    assert checked == ["AnswerDotAI/libx", "AnswerDotAI/appy", "AnswerDotAI/exty"]  # at root: full sweep

    checked.clear()
    (tmp_path/"appy"/"sub").mkdir()
    monkeypatch.chdir(tmp_path/"appy"/"sub")
    run()
    assert checked == ["AnswerDotAI/libx", "AnswerDotAI/appy"]  # inside a member: its closure, dir<->package mapped

    checked.clear()
    run(nodeps=True)
    assert checked == ["AnswerDotAI/appy"]  # nodeps: the repo alone

    checked.clear()
    monkeypatch.chdir(tmp_path)
    run(project="appy")
    assert checked == ["AnswerDotAI/libx", "AnswerDotAI/appy"]  # explicit project: closure, as before

    checked.clear()
    run(project="libx")
    assert checked == ["AnswerDotAI/libx"]  # explicit project by dir name maps to its package

    checked.clear()
    run(project="appy", nodeps=True)
    assert checked == ["AnswerDotAI/appy"]

    with pytest.raises(SystemExit): run(nodeps=True)  # no project in play
