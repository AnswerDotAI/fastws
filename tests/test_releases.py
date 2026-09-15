from fastcore.basics import AttrDict

import fastws.releases as relmod


def _rel(tag): return AttrDict(tag_name=tag)


def test_newest_tag_by_version_not_publish_order():
    assert relmod._newest_tag([_rel("v0.1.17"), _rel("v0.1.18"), _rel("v0.1.9")]) == "v0.1.18"
    assert relmod._newest_tag([]) is None


def test_skip_pats_defaults_and_config(tmp_path):
    (tmp_path/"pyproject.toml").write_text('[tool.fastws]\nrelease_skip = ["docs only"]\n')
    pats = relmod._skip_pats("wip", root=tmp_path)
    hits = lambda m: any(p.match(m) for p in pats)
    assert all(hits(m) for m in ("bump", "docs only: fix typo", "wip checkpoint"))
    assert not any(hits(m) for m in ("bumpy road ahead", "fixes #30"))


def test_release_report_repr():
    rep = relmod.ReleaseReport([("mdhtml", ["fixes #30"]), ("solveit", None), ("fastcore", []), ("bad", ValueError("boom"))])
    txt = repr(rep)
    assert "mdhtml (1 unreleased):" in txt and "  - fixes #30" in txt
    assert "no releases: solveit" in txt
    assert "up to date: fastcore" in txt
    assert "bad: ERROR boom" in txt
