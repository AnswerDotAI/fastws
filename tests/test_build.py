import os, time
import pytest, fastws.core as core


def _mk_proj(root, name, version="0.1.0"):
    d = root/name
    d.mkdir()
    (d/"pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n')
    (d/(name.replace("-", "_") + ".py")).write_text("x = 1\n")
    return d


def test_ws_build_incremental(tmp_path, capsys):
    a = _mk_proj(tmp_path, "proj-a")
    _mk_proj(tmp_path, "projb")
    out = tmp_path/".dists"

    core.ws_build(str(tmp_path))
    cap = capsys.readouterr()
    assert cap.out.strip() == str(out)
    assert "2 built" in cap.err
    assert sorted(p.name for p in out.glob("*.tar.gz")) == ["proj_a-0.1.0.tar.gz", "projb-0.1.0.tar.gz"]

    mtimes = {p.name: p.stat().st_mtime for p in out.glob("*.tar.gz")}
    core.ws_build(str(tmp_path))
    assert {p.name: p.stat().st_mtime for p in out.glob("*.tar.gz")} == mtimes

    future = time.time() + 5
    os.utime(a/"proj_a.py", (future, future))
    core.ws_build(str(tmp_path))
    m2 = {p.name: p.stat().st_mtime for p in out.glob("*.tar.gz")}
    assert m2["proj_a-0.1.0.tar.gz"] > mtimes["proj_a-0.1.0.tar.gz"]
    assert m2["projb-0.1.0.tar.gz"] == mtimes["projb-0.1.0.tar.gz"]

    (a/"pyproject.toml").write_text((a/"pyproject.toml").read_text().replace("0.1.0", "0.2.0"))
    os.utime(a/"pyproject.toml", (future + 5, future + 5))
    core.ws_build(str(tmp_path))
    names = sorted(p.name for p in out.glob("*.tar.gz"))
    assert names == ["proj_a-0.2.0.tar.gz", "projb-0.1.0.tar.gz"]

    m3 = {p.name: p.stat().st_mtime for p in out.glob("*.tar.gz")}
    core.ws_build(str(tmp_path), force=True)
    m4 = {p.name: p.stat().st_mtime for p in out.glob("*.tar.gz")}
    assert all(m4[k] > m3[k] for k in m3)

    bad = _mk_proj(tmp_path, "projc")
    (bad/"pyproject.toml").write_text('[project]\nname = "projc"\nversion = "0.1.0"\n[build-system]\nrequires = []\nbuild-backend = "no.such.backend"\n')
    with pytest.raises(SystemExit): core.ws_build(str(tmp_path))
    assert not list(out.glob("projc*"))
    assert sorted(p.name for p in out.glob("*.tar.gz")) == ["proj_a-0.2.0.tar.gz", "projb-0.1.0.tar.gz"]


def test_build_dependency_selection(tmp_path):
    root = tmp_path/'ws'
    root.mkdir()
    app = _mk_proj(root, 'app')
    lib = _mk_proj(root, 'my-lib')
    external = _mk_proj(tmp_path, 'external')
    _mk_proj(root, 'unrelated')
    (root/'repos-local.txt').write_text(f'owner/external {external}\n')
    (app/'pyproject.toml').write_text('[project]\nname="app"\ndependencies=["My.Lib>=1"]\n')
    (lib/'pyproject.toml').write_text('[project]\nname="my-lib"\n[build-system]\nrequires=["external"]\n')
    (external/'pyproject.toml').write_text('[project]\nname="external"\ndependencies=["app", "published-only"]\n')
    assert {n for n,d in core._build_projects(root, 'repos.txt', 'APP')} == {'app', 'my-lib', 'external'}
    assert len(core._build_projects(root, 'repos.txt')) == 4
    with pytest.raises(SystemExit, match='No workspace project'): core._build_projects(root, 'repos.txt', 'missing')
