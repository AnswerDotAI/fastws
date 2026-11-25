import json, shutil, subprocess, tarfile, pytest, fastws.core as core
from fastws.cargo import build, check, run_tests, Suite, _zig_target
from pathlib import Path


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
    app = _mk_proj(root, 'app', r'''dependencies = ["My.Lib>=1", "unused-wasm"]
[tool.fastws]
js-packages = ["frontend"]''')
    _mk_proj(root, 'my-lib', r'''[build-system]
requires = ["external"]''')
    external = _mk_proj(tmp_path, 'external', r'''dependencies = ["app", "published-only"]''')
    used_wasm = _mk_proj(root, 'used-wasm', r'''[tool.fastws]
wasm-package = { name = "used-wasm-js", path = "wasm/pkg" }''')
    root_wasm = _mk_proj(root, 'root-wasm', r'''[tool.fastws]
wasm-package = { name = "root-wasm-js", path = "wasm/pkg" }''')
    (root_wasm/'package.json').write_text('{"name":"root-wasm-js","version":"0.1.0","private":true}')
    (root_wasm/'wasm'/'pkg').mkdir(parents=True)
    (root_wasm/'wasm'/'pkg'/'package.json').write_text('{"name":"root-wasm-js","version":"0.1.0"}')
    (root/'pnpm-workspace.yaml').write_text('packages:\n  - root-wasm/wasm/pkg\n')
    unused_wasm = _mk_proj(root, 'unused-wasm', r'''[tool.fastws]
wasm-package = { name = "@example/unused-wasm", path = "wasm/pkg" }''')
    _mk_proj(root, 'unrelated')
    (root/'repos-local.txt').write_text(f'owner/external {external}')
    frontend = app/'frontend'
    frontend.mkdir()
    (frontend/'package.json').write_text('{"name":"app-fe", "dependencies":{"@example/assets":"^1", "used-wasm-js":">=0.1.0", "root-wasm-js":">=0.1.0"}}')
    assets = root/'tools'/'assets'
    assets.mkdir(parents=True)
    (assets.parent/'package.json').write_text('{"private":true,"workspaces":["assets"]}')
    (assets/'package.json').write_text('{"name":"@example/assets","version":"1.0.0"}')
    members = core._js_dirs(root)
    assert root_wasm.resolve() in members and (root_wasm/'wasm/pkg').resolve() not in members
    py, packages, _ = core._build_selection(root, 'repos.txt', 'APP')
    assert {n for n,d in py} == {'app', 'my-lib', 'external', 'unused-wasm', 'root-wasm'}
    assert set(packages) == {frontend, assets, root_wasm.resolve(), (used_wasm/'wasm/pkg').resolve()}
    assert (unused_wasm/'wasm/pkg').resolve() not in packages
    assert set(core._build_selection(root, 'repos.txt', '@example/unused-wasm')[1]) == {(unused_wasm/'wasm/pkg').resolve()}
    assert set(core._build_selection(root, 'repos.txt', '@example/assets')[1]) == {assets}
    with pytest.raises(SystemExit, match='No workspace project'): core._build_selection(root, 'repos.txt', 'missing')


def _write_cached_vp_shared(root):
    vp = Path(shutil.which('vp')).resolve().parent.parent
    pm = json.loads((vp/'package.json').read_text())['packageManager']
    node_vers = sorted(p.name for p in (vp.parent/'js_runtime'/'node').iterdir() if p.is_dir())
    (root / 'package.json.shared').write_text(json.dumps({'packageManager': pm,
        'devEngines': {'runtime': {'name': 'node', 'version': node_vers[-1]}}}))



def _archive_text(path, suffix):
    with tarfile.open(path) as archive:
        name = next(n for n in archive.getnames() if n.endswith(suffix))
        return archive.extractfile(name).read().decode()


@pytest.mark.skipif(not shutil.which('vp'), reason='JavaScript builds need Vite+ (vp)')
def test_mixed_distributions(tmp_path, capfd):
    app, styles, out = tmp_path/'app', tmp_path/'styles', tmp_path/'.dists'
    for d in app, styles: d.mkdir()
    lock = tmp_path/'pnpm-lock.yaml'
    lock.write_text('lockfileVersion: "9.0"\npackages:\n  theme@1.0.0: {}\n')
    (app/'pyproject.toml').write_text(r'''[project]
name = "office-app"
version = "0.1.0"
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"
[tool.setuptools]
py-modules = ["office_app"]
[tool.fastws]
js-packages = ["frontend"]
''')
    (app/'office_app.py').write_text('')
    (app/'MANIFEST.in').write_text('include dist/style.txt')
    (styles/'source.txt').write_text('first')
    (styles/'build.js').write_text(r'''const fs = require('node:fs');
const text = fs.readFileSync('source.txt', 'utf8');
if (!fs.existsSync('generated.txt') || fs.readFileSync('generated.txt', 'utf8') !== text) fs.writeFileSync('generated.txt', text);
fs.writeFileSync('_modidx.py', '# generated metadata');
console.log('exported styles');
''')
    (app/'build.js').write_text(r'''const fs = require('node:fs');
fs.mkdirSync('dist', {recursive:true});
const version = fs.readFileSync('../pnpm-lock.yaml', 'utf8').match(/theme@([^\s:]+)/)[1];
fs.writeFileSync('dist/style.txt', fs.readFileSync('../styles/generated.txt', 'utf8') + ':' + version);
console.log('bundled app');
''')
    style_pkg = dict(name='@example/styles', version='0.1.0', files=['generated.txt'], scripts={'build':'node build.js'})
    (styles/'package.json').write_text(json.dumps(style_pkg))
    (app/'package.json').write_text(json.dumps(dict(name='@example/app', version='0.1.0', files=['dist'], dependencies={'@example/styles':'^0.1.0'}, scripts={'build':'node build.js'})))
    (app/'frontend').mkdir()
    (app/'frontend'/'package.json').write_text(json.dumps(dict(name='priv-fe', version='0.0.0', private=True,
        scripts={'build': 'node -e "require(\'node:fs\').writeFileSync(\'built.txt\', \'ok\')"'})))
    _write_cached_vp_shared(tmp_path)
    core._sync_ws_pnpm_workspace(tmp_path, core._js_dirs(tmp_path))
    core._sync_ws_package_json(tmp_path)
    def build(): core.ws_build(workspace=str(tmp_path), project='office-app', workers=2)
    build()
    assert capfd.readouterr().out == f'{out}\n'
    sdist, = out.glob('*.tar.gz')
    app_tgz = out/'example-app-0.1.0.tgz'
    styles_tgz = out/'example-styles-0.1.0.tgz'
    assert _archive_text(sdist, 'dist/style.txt') == _archive_text(app_tgz, 'dist/style.txt') == 'first:1.0.0'
    assert set(out.glob('*.tgz')) == {app_tgz, styles_tgz} and (app/'frontend'/'built.txt').read_text() == 'ok' 

    # A dependency edit invalidates both generated app distributions, even though dist/ is a build output.
    (styles/'source.txt').write_text('second')
    build()
    assert _archive_text(sdist, 'dist/style.txt') == _archive_text(app_tgz, 'dist/style.txt') == 'second:1.0.0'
    lock.write_text(lock.read_text().replace('1.0.0', '1.0.1'))
    build()
    assert _archive_text(sdist, 'dist/style.txt') == _archive_text(app_tgz, 'dist/style.txt') == 'second:1.0.1'
    before = {p:p.stat().st_mtime_ns for p in out.iterdir()}
    build()
    assert before == {p:p.stat().st_mtime_ns for p in out.iterdir()}

    # pnpm writes its tarball before postpack. A failed attempt must not replace the last successful artifact.
    style_pkg['scripts']['postpack'] = 'node -e "process.exit(2)"'
    (styles/'package.json').write_text(json.dumps(style_pkg))
    with pytest.raises(SystemExit): build()
    assert styles_tgz.stat().st_mtime_ns == before[styles_tgz]
    assert not list(out.glob('.fastws-*'))


@pytest.mark.parametrize('body, diagnostic', [('', ''), ('let unused = 1;', 'warning'), ('compile_error!("broken");', 'error')])
def test_build_diagnostics(tmp_path, capfd, monkeypatch, body, diagnostic):
    (tmp_path/'pyproject.toml').write_text('[tool.maturin]\n[tool.fastws]\ncargo = '+json.dumps(shutil.which('cargo'))+'\n')
    monkeypatch.delenv('UV_PROJECT_ENVIRONMENT', raising=False)
    monkeypatch.setenv('VIRTUAL_ENV', str(tmp_path/'.venv'))
    (tmp_path/'Cargo.toml').write_text('[package]\nname = "app"\nversion = "0.1.0"\n[dependencies]\ndep = { path = "dep" }\n')
    (tmp_path/'src').mkdir()
    (tmp_path/'src/lib.rs').write_text('extern crate dep; pub use dep::run;')
    (tmp_path/'dep/src').mkdir(parents=True)
    (tmp_path/'dep/Cargo.toml').write_text('[package]\nname = "dep"\nversion = "0.1.0"\n')
    (tmp_path/'dep/src/lib.rs').write_text(f'pub fn run() {{ {body} }}')
    (tmp_path/'.cargo').mkdir()
    (tmp_path/'.cargo/config.toml').write_text('[term]\nquiet = true\n')
    if diagnostic == 'error':
        with pytest.raises(subprocess.CalledProcessError): build(tmp_path)
    else: assert [m['target']['name'] for m in build(tmp_path)] == ['app']
    stderr = capfd.readouterr().err
    if diagnostic: assert f'[dep] dep/src/lib.rs:1:' in stderr and f': {diagnostic}:' in stderr
    else: assert 'lib.rs:' not in stderr
    checked = check(tmp_path)
    assert checked.ok == (diagnostic != 'error') and [(d.path, d.level) for d in checked.diagnostics] == ([('dep/src/lib.rs', diagnostic)] if diagnostic else [])


def _test_crate(root):
    (root/'Cargo.toml').write_text('[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n')
    (root/'src').mkdir()
    (root/'src/lib.rs').write_text('''pub fn unused() { let x = 1; }
pub fn two() -> i32 { 2 }
#[cfg(test)] mod t {
    #[test] fn passes() {}
    #[test] fn fails() { panic!("broken"); }
    #[test] #[ignore] fn skipped() {
        println!("started slow test");
        std::thread::sleep(std::time::Duration::from_secs(60));
    }
}
''')
    (root/'tests').mkdir()
    (root/'tests/more.rs').write_text('#[test] fn other() { assert_eq!(app::two(), 2); }\n')
    (root/'.cargo').mkdir()
    (root/'.cargo/config.toml').write_text('[term]\nquiet = true\n')


def test_run_tests(tmp_path):
    _test_crate(tmp_path)
    r = run_tests(tmp_path, '--no-fail-fast')
    assert not r.ok
    assert [(d.path, d.line, d.level, d.code) for d in r.diagnostics] == [('src/lib.rs', 1, 'warning', 'unused_variables')]
    suites = {s.name: s for s in r.suites}
    unit, more = suites['unittests src/lib.rs'], suites['tests/more.rs']
    assert len(suites) == 2
    assert unit.tests == {'t::passes': 'ok', 't::fails': 'FAILED', 't::skipped': 'ignored'} and unit.failed
    assert 'broken' in unit.failures['t::fails'] and list(unit.failures) == ['t::fails']
    assert more.tests == {'other': 'ok'} and not more.failed
    slow = run_tests(tmp_path, '--lib', test_args=('skipped', '--ignored', '--nocapture'), timeout=.5)
    s, = slow.suites
    assert not slow.ok and s.failed and s.timed_out and 'started slow test' in s.output
    assert str(s) == 'unittests src/lib.rs: ran out of time'

    (tmp_path/'pyproject.toml').write_text(r'tool.fastws.dev-profile = "missing"')
    r = run_tests(tmp_path, '--test', 'more')
    assert not r.ok and not r.suites and 'missing' in r.text
    for args in [('--profile', 'test'), ('--profile=test',), ('--release',), ('-r',)]:
        assert run_tests(tmp_path, '--test', 'more', *args).ok


@pytest.mark.skipif(not shutil.which('cargo-nextest'), reason='needs cargo-nextest')
def test_run_tests_nextest(tmp_path, monkeypatch):
    _test_crate(tmp_path)
    (tmp_path/'pyproject.toml').write_text('[tool.fastws]\ncargo = '+json.dumps(shutil.which('cargo'))+'\n')
    monkeypatch.delenv('UV_PROJECT_ENVIRONMENT', raising=False)
    monkeypatch.setenv('VIRTUAL_ENV', str(tmp_path/'.venv'))
    (tmp_path/'.config').mkdir()
    (tmp_path/'.config/nextest.toml').write_text('[profile.default]\nfail-fast = false\n')
    r = run_tests(tmp_path)
    assert not r.ok and [(d.path, d.level) for d in r.diagnostics] == [('src/lib.rs', 'warning')]
    suites = {s.name: s for s in r.suites}
    unit, more = suites['unittests src/lib.rs'], suites['tests/more.rs']
    assert unit.tests == {'t::passes': 'ok', 't::fails': 'FAILED'} and unit.failed and 'broken' in unit.failures['t::fails']
    assert more.tests == {'other': 'ok'} and not more.failed
    assert run_tests(tmp_path, '--test', 'more', '--profile', 'test').ok
    slow = run_tests(tmp_path, '--lib', test_args=('skipped', '--ignored'), timeout=5)
    s, = slow.suites
    assert not slow.ok and s.timed_out and str(s) == 'nextest: ran out of time'

    # Editing the working tree after capture must not trigger another Cargo build.
    import fastws.cargo as cargo
    captured = cargo._nextest
    def edit_then_run(*args, **kwargs):
        (tmp_path/'src/lib.rs').write_text('compile_error!("changed after capture");')
        return captured(*args, **kwargs)
    monkeypatch.setattr(cargo, '_nextest', edit_then_run)
    assert run_tests(tmp_path, '--test', 'more').ok
    (tmp_path/'Cargo.toml').write_text('[package]')
    assert not run_tests(tmp_path).ok


@pytest.mark.skipif(not shutil.which('cargo-nextest'), reason='needs cargo-nextest')
def test_run_tests_profiles(tmp_path):
    a, b = tmp_path/'a', tmp_path/'b'
    for d in a, b: (d/'.config').mkdir(parents=True)
    _test_crate(a)
    (a/'.config/nextest.toml').write_text('[profile.one]\ndefault-filter = "test(passes) | test(other)"\n[profile.two]\ndefault-filter = "test(fails)"\n')
    (b/'Cargo.toml').write_text('[package]\nname = "b"\nversion = "0.1.0"\nedition = "2021"\n')
    (b/'src').mkdir()
    (b/'src/lib.rs').write_text('')
    (b/'tests').mkdir()
    (b/'tests/fine.rs').write_text('#[test] fn fine() {}\n')
    # Package filters use the all-feature graph, including dependencies disabled in this build.
    dep = b/'optional'
    (dep/'src').mkdir(parents=True)
    (dep/'Cargo.toml').write_text((b/'Cargo.toml').read_text().replace('name = "b"', 'name = "optional"'))
    (dep/'src/lib.rs').write_text('')
    with (b/'Cargo.toml').open('a') as f: f.write(r'''[workspace]
members = ["optional"]
default-members = ["."]
[dependencies]
optional = { path = "optional", optional = true }
''')
    (b/'.config/nextest.toml').write_text(r'''[profile.one]
default-filter = "rdeps(optional)"
''')
    r = run_tests([a, b], profiles=('one', 'two'))
    tests = {s.name: s.tests for s in r.suites if s.tests}
    assert not r.ok and tests == {'a one: unittests src/lib.rs': {'t::passes': 'ok'}, 'a one: tests/more.rs': {'other': 'ok'},
        'b one: tests/fine.rs': {'fine': 'ok'}, 'a two: unittests src/lib.rs': {'t::fails': 'FAILED'}}, r.text
    first = run_tests([a, b], profiles=('two', 'one'))
    assert not first.ok and {s.name for s in first.suites if s.tests} == {'a two: unittests src/lib.rs'}


def test_captured_test_output():
    for failed in (False, True):
        lines = ['test passes ... ok', 'successes:', '---- passes stdout ----', 'printed', 'successes:', 'passes']
        if failed:
            lines[1:1] = ['test fails ... FAILED']
            lines += ['failures:', '---- fails stdout ----', 'broken', 'failures:', 'fails']
        lines.append(f'test result: {"FAILED" if failed else "ok"}. 1 passed; {int(failed)} failed; 0 ignored')
        s = Suite.read('unit', lines, int(failed))
        assert s.failures == ({'fails': 'broken'} if failed else {}) and 'printed' in s.output


def test_zig_target():
    t = 'x86_64-unknown-linux-gnu'
    assert _zig_target(t, '2_31') == _zig_target(t, '2.31') == _zig_target(t, 'manylinux_2_31') == f'{t}.2.31'
    assert _zig_target(t, 'manylinux2014') == f'{t}.2.17'
    assert _zig_target(f'{t}.2.17', '2_31') == f'{t}.2.17'
    assert _zig_target('aarch64-apple-darwin', '2_31') == 'aarch64-apple-darwin'
    assert _zig_target(t, 'off') == _zig_target(t, 'auto') == _zig_target(t, 'musllinux_1_2') == t
