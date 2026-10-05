import pytest
import fastws.core as core


def _mk_proj(root, name, extra=''):
    d = root/name
    d.mkdir()
    (d/'pyproject.toml').write_text(fr'''[project]
name = "{name}"
{extra}
''')
    return d


def test_build_dependency_selection(tmp_path):
    root = tmp_path/'ws'
    root.mkdir()
    _mk_proj(root, 'app', r'''dependencies = ["My.Lib>=1"]''')
    _mk_proj(root, 'my-lib', r'''[build-system]
requires = ["external"]''')
    external = _mk_proj(tmp_path, 'external', r'''dependencies = ["app", "published-only"]''')
    _mk_proj(root, 'unrelated')
    (root/'repos-local.txt').write_text(f'owner/external {external}')
    assert {n for n,d in core._build_projects(root, 'repos.txt', 'APP')} == {'app', 'my-lib', 'external'}
    with pytest.raises(SystemExit, match='No workspace project'): core._build_projects(root, 'repos.txt', 'missing')
