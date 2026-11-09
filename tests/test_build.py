import json, shutil, subprocess, pytest, fastws.core as core
from fastws.cargo import build, check, run_tests, Suite


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
    (b/'.config/nextest.toml').write_text('[profile.one]\n')
    r = run_tests([a, b], profiles=('one', 'two'))
    tests = {s.name: s.tests for s in r.suites if s.tests}
    assert not r.ok and tests == {'a one: unittests src/lib.rs': {'t::passes': 'ok'}, 'a one: tests/more.rs': {'other': 'ok'},
                                  'b one: tests/fine.rs': {'fine': 'ok'}, 'a two: unittests src/lib.rs': {'t::fails': 'FAILED'}}
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
