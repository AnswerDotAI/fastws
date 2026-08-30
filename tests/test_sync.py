import os, pytest
from types import SimpleNamespace
from fastgit import Git
import fastws.core as core

os.environ.update(GIT_AUTHOR_NAME='fastws', GIT_AUTHOR_EMAIL='fastws@example.com',
    GIT_COMMITTER_NAME='fastws', GIT_COMMITTER_EMAIL='fastws@example.com')

WS_META = '[project]\nname = "uvws"\ndependencies = [\n    "repo1pkg",\n    "keeppkg",\n]\n\n[tool.uv.sources]\nrepo1pkg = { workspace = true }\nkeeppkg = { workspace = true }\n'


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


@pytest.fixture
def fake_uv(monkeypatch):
    "Intercept uv/cargo invocations (external tools stay out of tests); real git passes through"
    calls, real = [], core.subprocess.run
    def run(cmd, **kw):
        if cmd[0] in ('uv', 'cargo'):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return real(cmd, **kw)
    monkeypatch.setattr(core.subprocess, 'run', run)
    return calls


def test_repos_file_roundtrip(tmp_path):
    repos_path = tmp_path/'repos.txt'
    repos_path.write_text('# header\nAnswerDotAI/fastws\njph00/private ~/private\nfastai/fastai sub/dir\n')

    assert core._load_repos(repos_path) == ['AnswerDotAI/fastws', 'jph00/private', 'fastai/fastai']
    assert core._load_repo_entries(repos_path, tmp_path) == [
        ('AnswerDotAI/fastws', tmp_path/'fastws'),
        ('jph00/private', core.Path('~/private').expanduser()),
        ('fastai/fastai', tmp_path/'sub'/'dir')]

    # update appends only genuinely-new specs: case differences and location-carrying lines both count as present
    added = core._update_repos_file(repos_path, ['answerdotai/fastws', 'jph00/private', 'fastai/new'])
    assert added == ['fastai/new']
    assert repos_path.read_text().endswith('fastai/new\n')

    # remove matches case-insensitively, preserves other lines (including comments), and reports absence
    assert core._remove_from_repos_file(repos_path, 'FASTAI/new') is True
    assert core._remove_from_repos_file(repos_path, 'AnswerDotAI/gone') is False
    assert repos_path.read_text() == '# header\nAnswerDotAI/fastws\njph00/private ~/private\nfastai/fastai sub/dir\n'


def test_ws_pyproject_from_template_adds_projects(tmp_path):
    (tmp_path/'pyproject.tmpl').write_text('[project]\nname = "uvws"\ndependencies = [\n    "ipython>=8.34.0",\n]\n\n[tool.uv.sources]\n\n')
    for name in ('alpha', 'beta'):
        (tmp_path/name).mkdir()
        (tmp_path/name/'pyproject.toml').write_text(f'[project]\nname = "{name}"\n')

    added = core._sync_ws_pyproject(tmp_path/'pyproject.toml', tmp_path/'pyproject.tmpl', ['alpha', 'beta'])
    content = (tmp_path/'pyproject.toml').read_text()
    assert added == ['alpha', 'beta']
    for s in ('alpha = { workspace = true }', 'beta = { workspace = true }', '"alpha"', '"beta"', 'ipython>=8.34.0'): assert s in content

    added = core._sync_ws_pyproject(tmp_path/'pyproject.toml', tmp_path/'pyproject.tmpl', ['alpha', 'beta'])
    assert added == []
    assert (tmp_path/'pyproject.toml').read_text() == content


def test_ws_pyproject_preserves_existing_entries(tmp_path):
    # a case-only difference is the same dependency, and hand-written path sources survive syncs
    pyproject = tmp_path/'pyproject.toml'
    orig = '[project]\nname = "uvws"\ndependencies = ["FastWS"]\n\n[tool.uv.sources]\nFastWS = { workspace = true }\n'
    pyproject.write_text(orig)
    assert core._sync_ws_pyproject(pyproject, tmp_path/'pyproject.tmpl', ['fastws']) == []
    assert pyproject.read_text() == orig

    pyproject.write_text('[project]\nname = "uvws"\ndependencies = [\n    "mytool",\n]\n\n[tool.uv.sources]\nmytool = { path = "../private/mytool", editable = true }\n')
    (tmp_path/'alpha').mkdir()
    (tmp_path/'alpha'/'pyproject.toml').write_text('[project]\nname = "alpha"\n')
    assert core._sync_ws_pyproject(pyproject, tmp_path/'pyproject.tmpl', ['alpha']) == ['alpha']
    content = pyproject.read_text()
    assert 'mytool = { path = "../private/mytool", editable = true }' in content
    assert 'alpha = { workspace = true }' in content


def test_ws_excludes_generates_from_intent_and_auto(tmp_path):
    pyproject = tmp_path/'pyproject.toml'
    pyproject.write_text(
        '[project]\nname = "uvws"\n\n[tool.uv.workspace]\nmembers = ["./*"]\n'
        'exclude = ["_*", "tmp", "example", "stale", "pending"]\n\n[tool.fastws]\nexclude = ["wanted-out"]\n')
    for name in ('junk', 'fresh-clone', 'realpkg', 'tmpl', 'example', 'stale', 'wanted-out', 'pending', 'rustcrate'): (tmp_path/name).mkdir()
    (tmp_path/'realpkg'/'pyproject.toml').write_text('[project]\nname = "realpkg"\n')
    (tmp_path/'tmpl'/'pyproject.toml').write_text('[project]\nname = "{repo}"\n')
    (tmp_path/'rustcrate'/'Cargo.toml').write_text('[package]\nname = "rustcrate"\n')
    for name in ('example', 'stale', 'wanted-out'): (tmp_path/name/'pyproject.toml').write_text(f'[project]\nname = "{name}"\n')

    tracked = {'fresh-clone', 'example', 'pending', 'rustcrate'}
    added, removed = core._sync_ws_excludes(pyproject, tmp_path, tracked)
    data = core.tomllib.loads(pyproject.read_text())

    # kept: glob, missing dir, tracked non-project; auto: junk, placeholder tmpl, tracked Cargo-only crate; intent: wanted-out; dropped: valid projects, tracked or not
    assert set(data['tool']['uv']['workspace']['exclude']) == {'_*', 'tmp', 'pending', 'wanted-out', 'junk', 'tmpl', 'rustcrate'}
    assert set(added) == {'wanted-out', 'junk', 'tmpl', 'rustcrate'}
    assert set(removed) == {'example', 'stale'}
    assert data['tool']['fastws']['exclude'] == ['wanted-out']  # fastws table untouched

    content = pyproject.read_text()
    assert core._sync_ws_excludes(pyproject, tmp_path, tracked) == ([], [])
    assert pyproject.read_text() == content

    # a crate that gains a Python layer rejoins the workspace on the next sync
    (tmp_path/'rustcrate'/'pyproject.toml').write_text('[project]\nname = "rustcrate"\n')
    assert core._sync_ws_excludes(pyproject, tmp_path, tracked) == ([], ['rustcrate'])


def test_external_projects_discovers_root_and_subdir_packages(tmp_path):
    root = tmp_path/'ws'
    root.mkdir()
    single = tmp_path/'single'
    single.mkdir()
    (single/'pyproject.toml').write_text('[project]\nname = "singlepkg"\n')
    multi = tmp_path/'multi'
    (multi/'notes').mkdir(parents=True)
    for name, pkg in (('tool1', 'tool1'), ('tmpl', '{repo}')):
        (multi/name).mkdir()
        (multi/name/'pyproject.toml').write_text(f'[project]\nname = "{pkg}"\n')

    assert core._external_projects(root, [single, multi, tmp_path/'missing']) == [('singlepkg', '../single'), ('tool1', '../multi/tool1')]


def test_ws_projects_skip_excluded_dirs_and_template_names(tmp_path):
    (tmp_path/'pyproject.toml').write_text('[tool.uv.workspace]\nmembers = ["./*"]\nexclude = ["skip-*"]\n')
    for name, pkg in (('keep', 'keepme'), ('skip-template', 'skipme'), ('template', '{repo}')):
        (tmp_path/name).mkdir()
        (tmp_path/name/'pyproject.toml').write_text(f'[project]\nname = "{pkg}"\n')

    assert core._ws_projects(tmp_path) == ['keepme']


def test_remove_from_pyproject(tmp_path):
    pyproject = tmp_path/'pyproject.toml'
    pyproject.write_text('[project]\nname = "uvws"\ndependencies = [\n    "alpha",\n    "mytool",\n]\n\n[tool.uv.sources]\nalpha = { workspace = true }\nmytool = { path = "../private/mytool", editable = true }\n')

    assert core._remove_from_pyproject(pyproject, ['Alpha']) == ['alpha']  # case-insensitive
    content = pyproject.read_text()
    assert 'alpha' not in content
    assert 'mytool = { path = "../private/mytool", editable = true }' in content  # path sources survive

    assert core._remove_from_pyproject(pyproject, ['alpha']) == []
    assert pyproject.read_text() == content


async def test_git_repo_resolution(tmp_path):
    repo = tmp_path/'proj'
    repo.mkdir()
    (repo/'pyproject.toml').write_text('[project]\nname = "proj"\n')
    g = mk_repo(repo)

    with pytest.raises(SystemExit, match='origin'): core._origin_repo(repo)
    assert await core._discover_ws_repos(tmp_path) == []

    g.remote('add', 'origin', 'git@github.com:AnswerDotAI/Proj.git')
    assert core._origin_repo(repo) == 'AnswerDotAI/Proj'
    assert await core._discover_ws_repos(tmp_path) == ['AnswerDotAI/Proj']
    assert core._resolve_add_target(tmp_path, 'proj') == ('AnswerDotAI/Proj', True, None)

    # discovery covers every root git dir, even excluded non-Python ones; `_`-prefixed dirs are private
    crate = tmp_path/'crate'
    crate.mkdir()
    (crate/'Cargo.toml').write_text('[package]\nname = "crate"\n')
    mk_repo(crate).remote('add', 'origin', 'git@github.com:AnswerDotAI/crate.git')
    hidden = tmp_path/'_scratch'
    hidden.mkdir()
    mk_repo(hidden).remote('add', 'origin', 'git@github.com:AnswerDotAI/scratch.git')
    (tmp_path/'pyproject.toml').write_text('[tool.uv.workspace]\nmembers = ["./*"]\nexclude = ["crate"]\n')
    assert sorted(await core._discover_ws_repos(tmp_path)) == ['AnswerDotAI/Proj', 'AnswerDotAI/crate']


def test_ws_remove_workflow(tmp_path, monkeypatch, fake_uv):
    (tmp_path/'repos.txt').write_text('AnswerDotAI/keep\nAnswerDotAI/repo1\n')
    (tmp_path/'pyproject.toml').write_text(WS_META)
    repo = tmp_path/'repo1'
    repo.mkdir()
    (repo/'pyproject.toml').write_text('[project]\nname = "repo1pkg"\n')
    g = mk_repo(repo, origin=tmp_path/'origins'/'repo1')
    answer = ['y']
    monkeypatch.setattr('builtins.input', lambda *a: answer[0])

    # a dirty tree refuses before any mutation
    (repo/'pyproject.toml').write_text('[project]\nname = "repo1pkg"\nversion = "1"\n')
    with pytest.raises(SystemExit): core.ws_remove('AnswerDotAI/repo1', workspace=str(tmp_path))
    assert repo.exists() and 'repo1' in (tmp_path/'repos.txt').read_text()

    # so does an unpushed commit on a clean tree
    g.commit('-a', m='ahead')
    with pytest.raises(SystemExit): core.ws_remove('AnswerDotAI/repo1', workspace=str(tmp_path))
    assert repo.exists()

    # pushed and clean: answering 'n' keeps the directory but still removes the metadata and re-syncs
    g.push()
    answer[0] = 'n'
    core.ws_remove('AnswerDotAI/repo1', workspace=str(tmp_path))
    assert repo.exists()
    assert 'repo1' not in (tmp_path/'repos.txt').read_text()
    assert 'repo1pkg' not in (tmp_path/'pyproject.toml').read_text()
    assert ['uv', 'sync'] in fake_uv

    # answering 'y' (by folder name this time) also deletes the checkout
    (tmp_path/'repos.txt').write_text('AnswerDotAI/keep\nAnswerDotAI/repo1\n')
    (tmp_path/'pyproject.toml').write_text(WS_META)
    answer[0] = 'y'
    core.ws_remove('repo1', workspace=str(tmp_path))
    assert not repo.exists()
    assert (tmp_path/'repos.txt').read_text() == 'AnswerDotAI/keep\n'


def test_upgrade_stamp(tmp_path):
    assert core._should_upgrade(tmp_path)  # no stamp yet
    core._upgrade_stamp(tmp_path).touch()
    assert not core._should_upgrade(tmp_path)  # fresh stamp
    old = core.time.time() - 90000
    os.utime(core._upgrade_stamp(tmp_path), (old, old))
    assert core._should_upgrade(tmp_path)  # stamp >24h old


def test_cargo_keys_hash_contents_and_patched_git_deps(tmp_path):
    crate, dep = tmp_path/'crate', tmp_path/'dep'
    for d in crate, dep: (d/'src').mkdir(parents=True)
    (crate/'.git').mkdir()
    (tmp_path/'.cargo').mkdir()
    # exclude the crate from the uv workspace: crates are keyed regardless of uv membership
    (tmp_path/'pyproject.toml').write_text('[tool.uv.workspace]\nmembers = ["*"]\nexclude = ["crate"]\n')
    (tmp_path/'.cargo'/'config.toml').write_text(f'[patch."https://example.com/dep"]\ndep = {{ path = "{dep}" }}\n')
    (crate/'Cargo.toml').write_text('[package]\nname = "crate"\nversion = "0.1.0"\n\n[dependencies]\ndep = { git = "https://example.com/dep" }\n')
    lock = crate/'Cargo.lock'
    lock.write_text('first lock\n')
    (crate/'src'/'lib.rs').write_text('root source\n')
    (dep/'Cargo.toml').write_text('[package]\nname = "dep"\nversion = "0.1.0"\n')
    dep_src = dep/'src'/'lib.rs'
    dep_src.write_text('dependency source\n')

    core._sync_cargo_keys(tmp_path)
    key = crate/'.git'/'fastws-cargo-key'
    first, mtime = key.read_text(), key.stat().st_mtime_ns

    lock_mtime = lock.stat().st_mtime_ns
    os.utime(lock, ns=(lock_mtime + 1_000_000_000, lock_mtime + 1_000_000_000))
    core._sync_cargo_keys(tmp_path)
    assert key.read_text() == first and key.stat().st_mtime_ns == mtime  # mtime alone doesn't change the key

    lock.write_text('second lock\n')
    core._sync_cargo_keys(tmp_path)
    second = key.read_text()
    assert second != first  # lock content does

    dep_src.write_text('changed dependency source\n')
    core._sync_cargo_keys(tmp_path)
    assert key.read_text() != second  # so does a patched dep's source


def test_cargo_key_ignores_unused_patch_order(tmp_path):
    crate = tmp_path/'crate'
    (crate/'.git').mkdir(parents=True)
    (crate/'Cargo.toml').write_text('[package]\nname = "crate"\n')
    lock = crate/'Cargo.lock'
    head = 'version = 4\n\n[[package]]\nname = "crate"\nversion = "0.1.0"\n\n'
    a = '[[patch.unused]]\nname = "a"\nversion = "1"\n\n'
    b = '[[patch.unused]]\nname = "b"\nversion = "1"\n'
    lock.write_text(head+a+b)
    core._sync_cargo_keys(tmp_path)
    key = crate/'.git'/'fastws-cargo-key'
    first, mtime = key.read_text(), key.stat().st_mtime_ns

    lock.write_text(head+b+'\n'+a)
    core._sync_cargo_keys(tmp_path)
    assert key.read_text() == first and key.stat().st_mtime_ns == mtime

    lock.write_text(head.replace('0.1.0', '0.1.1')+a+b)
    core._sync_cargo_keys(tmp_path)
    assert key.read_text() != first


async def test_changed_dirs_pulls_only_moved_repos(tmp_path, monkeypatch):
    # ahead: a second clone pushes to the shared bare origin, so `ahead`'s origin/main falls behind
    ahead, current, weird = tmp_path/'ahead', tmp_path/'current', tmp_path/'weird'
    for d in (ahead, current):
        d.mkdir()
        g = mk_repo(d, origin=tmp_path/'origins'/d.name)
        g.remote('set-url', '--push', 'origin', str(tmp_path/'origins'/d.name))
    other = tmp_path/'other'
    Git(tmp_path, raise_exc=True).clone(str(tmp_path/'origins'/'ahead'), str(other))
    og = Git(other, raise_exc=True)
    (other/'g.txt').write_text('new')
    og.add('.')
    og.commit(m='advance')
    og.push()
    weird.mkdir()
    mk_repo(weird)  # no origin remote: can't be checked, so it must be pulled

    async def fake_heads(refs): return [str(Git(tmp_path/'origins'/spec.split('/')[1], raise_exc=True).rev_parse(branch)) for spec, branch in refs]
    monkeypatch.setattr(core, '_remote_heads', fake_heads)
    monkeypatch.setattr(core, '_parse_github_repo', lambda url: f'o/{core.Path(url).name}' if 'origins' in url else None)

    changed = await core._changed_dirs([ahead, current, weird])
    assert set(changed) == {ahead, weird}


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
    (family/'Cargo.toml').write_text('[package]\nname = "family"\nversion = "0.1.0"\n\n[workspace]\nmembers = ["sub"]\n')
    (family/'sub'/'Cargo.toml').write_text('[package]\nname = "family-sub"\nversion = "0.1.0"\n')
    scratch = tmp_path/'_scratch'
    scratch.mkdir()
    (scratch/'Cargo.toml').write_text('[package]\nname = "scratch"\n')

    added, removed = core._sync_cargo_patches(tmp_path)
    data = core.tomllib.loads(config.read_text())
    cio = data['patch']['crates-io']
    # every local crate patched, nested cargo workspace members included, `_`-prefixed dirs skipped
    assert set(cio) == {'foreign', 'crate1', 'family', 'family-sub'}
    assert cio['foreign']['path'] == '/elsewhere/foreign'  # entries pointing outside the root are kept as-is
    assert cio['crate1']['path'] == str(crate1)
    assert cio['family-sub']['path'] == str(family/'sub')
    assert data['patch']['https://example.com/family']['family']['path'] == str(family)  # git deps on local crates get their URL table
    assert data['term']['quiet'] is True  # other sections untouched
    assert set(added) == {'crate1', 'family', 'family-sub'} and removed == ['gone']

    content = config.read_text()
    assert core._sync_cargo_patches(tmp_path) == ([], [])
    assert config.read_text() == content

    # a missing config file is created
    bare = tmp_path/'ws2'
    (bare/'c').mkdir(parents=True)
    (bare/'c'/'Cargo.toml').write_text('[package]\nname = "c"\n')
    core._sync_cargo_patches(bare)
    assert core.tomllib.loads((bare/'.cargo'/'config.toml').read_text())['patch']['crates-io']['c']['path'] == str(bare/'c')


def test_sync_cargo_wrapper(tmp_path, monkeypatch):
    config = tmp_path/'.cargo'/'config.toml'
    config.parent.mkdir()
    config.write_text('[term]\nquiet = true\n')
    monkeypatch.setattr(core.shutil, 'which', lambda name: '/opt/homebrew/bin/sccache')

    assert core._sync_cargo_wrapper(tmp_path)
    data = core.tomllib.loads(config.read_text())
    assert data['build']['rustc-wrapper'] == '/opt/homebrew/bin/sccache'
    assert data['term']['quiet'] is True
    assert not core._sync_cargo_wrapper(tmp_path)

    (tmp_path/'crate').mkdir()
    (tmp_path/'crate'/'Cargo.toml').write_text('[package]\nname = "crate"\n')
    core._sync_cargo_patches(tmp_path)
    content = config.read_text()
    assert not core._sync_cargo_wrapper(tmp_path)
    assert core._sync_cargo_patches(tmp_path) == ([], []) and config.read_text() == content

    config.write_text('[build]\nrustc-wrapper = "other-cache"\n')
    assert not core._sync_cargo_wrapper(tmp_path)
    assert core.tomllib.loads(config.read_text())['build']['rustc-wrapper'] == 'other-cache'
