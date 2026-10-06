import json, os, pytest
from fastgit import Git
import fastws.core as core

os.environ.update(GIT_AUTHOR_NAME='fastws', GIT_AUTHOR_EMAIL='fastws@example.com',
    GIT_COMMITTER_NAME='fastws', GIT_COMMITTER_EMAIL='fastws@example.com')

WS_META = '[project]\nname = "uvws"\ndependencies = [\n    "repo1pkg",\n    "repo2pkg",\n    "keeppkg",\n]\n\n[tool.uv.sources]\nrepo1pkg = { workspace = true }\nrepo2pkg = { workspace = true }\nkeeppkg = { workspace = true }\n'


def mk_repo(d, origin=None):
    "Real git repo at `d`, committing any existing files; with `origin`, a bare remote with main pushed"
    d.mkdir(exist_ok=True)
    g = Git(d, raise_exc=True)
    g.init(b='main')
    if not [p for p in d.iterdir() if p.name != '.git']: (d/'f.txt').write_text('x')
    g.add('.')
    g.commit(m='init')
    if origin:
        origin.mkdir(parents=True)
        Git(origin, raise_exc=True).init(bare=True)
        g.remote('add', 'origin', str(origin))
        g.push('-u', 'origin', 'main')
    return g


def test_repos_file_roundtrip(tmp_path):
    repos_path = tmp_path/'repos.txt'
    repos_path.write_text('# header\nAnswerDotAI/fastws[dev]\njph00/private[docs] ~/private\nfastai/fastai sub/dir\n')

    assert core._load_repo_entries(repos_path, tmp_path) == [
        ('AnswerDotAI/fastws', tmp_path/'fastws', {'dev'}),
        ('jph00/private', core.Path('~/private').expanduser(), {'docs'}),
        ('fastai/fastai', tmp_path/'sub'/'dir', set())]

    # discovery and ws-add write only extras, never change the shared baseline
    original = repos_path.read_text()
    local = tmp_path/'repos-local.txt'
    added = core._update_repos_file(repos_path, ['answerdotai/fastws', 'jph00/private', 'fastai/new[dev]'])
    assert added == ['fastai/new[dev]']
    assert local.read_text() == 'fastai/new[dev]\n'
    assert repos_path.read_text() == original
    assert core._load_repo_entries(repos_path, tmp_path)[-1] == ('fastai/new', tmp_path/'new', {'dev'})

    # remove matches case-insensitively, preserves other lines (including comments), and reports absence
    assert core._remove_from_repos_file(local, 'FASTAI/new') is True
    assert core._remove_from_repos_file(repos_path, 'AnswerDotAI/gone') is False
    assert repos_path.read_text() == original


def test_repo_lists_deduplicate_and_reject_conflicting_locations(tmp_path):
    base, local = tmp_path/'repos.txt', tmp_path/'repos-local.txt'
    base.write_text('org/core[dev]\norg/extra[docs] ../external\n')
    local.write_text('ORG/core[TEST, dev]\norg/extra[dev]\n')
    assert core._load_repo_entries(base, tmp_path) == [
        ('org/core', tmp_path/'core', {'dev', 'test'}), ('org/extra', tmp_path/'../external', {'docs', 'dev'})]
    local.write_text('org/core ../elsewhere\n')
    with pytest.raises(SystemExit, match='location'): core._load_repo_entries(base, tmp_path)
    local.write_text('different/core\n')
    with pytest.raises(SystemExit, match='location'): core._load_repo_entries(base, tmp_path)
    base.unlink()
    assert core._load_repo_entries(base, tmp_path) == [('different/core', tmp_path/'core', set())]


@pytest.mark.parametrize('entry', ['org/repo[dev', 'org/repo[dev]]', 'org/repo[]'])
def test_invalid_repo_extras(entry):
    with pytest.raises(SystemExit, match='Invalid repo entry'): core._parse_repo_line(entry)


def test_sync_extras_preserves_constraints_markers_and_unrelated_settings(tmp_path):
    pyproject = tmp_path/'pyproject.toml'
    pyproject.write_text('[project]\nname = "ws"\ndependencies = [\'My_Pkg[old]>=1; python_version >= "3.10"\', "other[keep]"]\n'
        '[tool.uv.sources]\nMy_Pkg = { path = "../somewhere", editable = true }\n')
    for extras in ({'dev', 'test'}, set()):
        core._sync_ws_pyproject(pyproject, tmp_path/'pyproject.tmpl', ['my-pkg'], extras={'my-pkg': extras})
        data = core.tomllib.loads(pyproject.read_text())
        dep, other = data['project']['dependencies']
        req = core.Requirement(dep)
        assert req.extras == extras and str(req.specifier) == '>=1' and str(req.marker) == 'python_version >= "3.10"'
        assert other == 'other[keep]' and len(data['project']['dependencies']) == 2
        assert data['tool']['uv']['sources'] == {'My_Pkg': {'path': '../somewhere', 'editable': True}}


def test_remove_from_pyproject(tmp_path):
    pyproject = tmp_path/'pyproject.toml'
    pyproject.write_text('[project]\nname = "uvws"\ndependencies = [\n    "alpha",\n    "mytool",\n]\n\n[tool.uv.sources]\nalpha = { workspace = true }\nmytool = { path = "../private/mytool", editable = true }\n')

    assert core._remove_from_pyproject(pyproject, ['Alpha']) == ['alpha']  # case-insensitive
    content = pyproject.read_text()
    assert 'alpha' not in content
    assert 'mytool = { path = "../private/mytool", editable = true }' in content  # path sources survive


def test_ws_remove_refuses_unsafe_repos(tmp_path):
    (tmp_path/'repos.txt').write_text('AnswerDotAI/keep\n')
    local = tmp_path/'repos-local.txt'
    local.write_text('AnswerDotAI/repo1\nAnswerDotAI/repo2\norg/external ../external\n')
    (tmp_path/'pyproject.toml').write_text(WS_META)
    repo, repo2 = tmp_path/'repo1', tmp_path/'repo2'
    for d in repo, repo2:
        d.mkdir()
        (d/'pyproject.toml').write_text(f'[project]\nname = "{d.name}pkg"\n')
        g = mk_repo(d, origin=tmp_path/'origins'/d.name)
    with pytest.raises(SystemExit, match='baseline'): core.ws_remove('repo1', 'AnswerDotAI/keep', workspace=str(tmp_path))
    with pytest.raises(SystemExit, match='location'): core.ws_remove('repo1', 'org/external', workspace=str(tmp_path))

    (repo2/'pyproject.toml').write_text('[project]\nname = "repo2pkg"\nversion = "1"\n')
    with pytest.raises(SystemExit, match='uncommitted'): core.ws_remove('repo1', 'repo2', workspace=str(tmp_path))
    g.commit('-a', m='ahead')
    with pytest.raises(SystemExit, match='unpushed'): core.ws_remove('repo1', 'repo2', workspace=str(tmp_path))
    assert repo.exists() and repo2.exists()
    assert local.read_text() == 'AnswerDotAI/repo1\nAnswerDotAI/repo2\norg/external ../external\n'
    assert (tmp_path/'pyproject.toml').read_text() == WS_META
    assert (tmp_path/'repos.txt').read_text() == 'AnswerDotAI/keep\n'


def test_cargo_key_hashes_lock_and_patched_dependency_contents(tmp_path):
    crate, dep = tmp_path/'crate', tmp_path/'dep'
    for d in crate, dep: (d/'src').mkdir(parents=True)
    (crate/'Cargo.toml').write_text(r'''[dependencies]
dep = { git = "https://example.com/dep" }
''')
    (dep/'Cargo.toml').touch()
    source, lock = dep/'src'/'lib.rs', crate/'Cargo.lock'
    source.write_text('original')
    lock.write_text('first lock')
    patches = {('https://example.com/dep', 'dep'): dep}
    first = core._cargo_key(crate, patches, None)
    lock.write_text('second lock')
    second = core._cargo_key(crate, patches, None)
    assert second != first
    source.write_text('changed')
    assert core._cargo_key(crate, patches, None) != second


def test_cargo_lock_ignores_unused_patches(tmp_path):
    lock = tmp_path/'Cargo.lock'
    header = r'''version = 4
[[package]]
name = "crate"
'''
    lock.write_text(header + r'''[[patch.unused]]
name = "unused"
''')
    assert core._cargo_lock_content(lock) == header.encode()


def test_sync_cargo_patches_generates_and_preserves(tmp_path):
    (tmp_path/'.cargo').mkdir()
    config = tmp_path/'.cargo'/'config.toml'
    config.write_text('[term]\nquiet = true\n\n[patch.crates-io]\n'
        f'foreign = {{ path = "/elsewhere/foreign" }}\ngone = {{ path = "{tmp_path}/gone" }}\n')
    crate1 = tmp_path/'crate1'
    crate1.mkdir()
    (crate1/'Cargo.toml').write_text('[package]\nname = "crate1"\nversion = "0.1.0"\n\n[dependencies]\nfamily = { git = "https://example.com/family" }\n')
    family = tmp_path/'family'
    (family/'sub').mkdir(parents=True)
    (family/'Cargo.toml').write_text('[package]\nname = "family"\nversion = "0.1.0"\npublish = false\n\n[workspace]\nmembers = ["sub"]\n')
    (family/'sub'/'Cargo.toml').write_text('[package]\nname = "family-sub"\nversion = "0.1.0"\n')

    added, removed = core._sync_cargo_patches(tmp_path)
    data = core.tomllib.loads(config.read_text())
    cio = data['patch']['crates-io']
    assert set(cio) == {'foreign', 'crate1', 'family-sub'}  # `publish = false` crates never come from crates.io
    assert cio['foreign']['path'] == '/elsewhere/foreign'  # entries pointing outside the root are kept as-is
    assert cio['crate1']['path'] == str(crate1)
    assert cio['family-sub']['path'] == str(family/'sub')
    assert data['patch']['https://example.com/family']['family']['path'] == str(family)  # git deps on local crates get their URL table, published or not
    assert data['term']['quiet'] is True  # other sections untouched
    assert set(added) == {'crate1', 'family', 'family-sub'} and removed == ['gone']


def test_sync_ws_package_json_preserves_local_and_merges_shared(tmp_path):
    pkg, shared = tmp_path/'package.json', tmp_path/'package.json.shared'
    pkg.write_text(json.dumps({'name': 'local', 'workspaces': ['../outside', 'gone', 'tools/*'], 'allowScripts': {'other': False}}))
    shared.write_text('{"allowScripts": {"wasm-pack": true}}')
    assert core._sync_ws_package_json(tmp_path, [tmp_path/'app']) == (['app'], ['gone'])
    assert json.loads(pkg.read_text()) == {'name': 'local', 'workspaces': ['../outside', 'tools/*', 'app'],
        'allowScripts': {'other': False, 'wasm-pack': True}}



