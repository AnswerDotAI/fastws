import subprocess, pytest, fastws.core as core
from fastws.cargo import build, run_tests, diagnostics


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
def test_build_diagnostics(tmp_path, capfd, body, diagnostic):
    (tmp_path/'pyproject.toml').write_text('[tool.maturin]\n')
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
    else: assert not diagnostics(stderr)


def test_run_tests(tmp_path):
    (tmp_path/'Cargo.toml').write_text('[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n')
    (tmp_path/'src').mkdir()
    (tmp_path/'src/lib.rs').write_text('''pub fn unused() { let x = 1; }
/// ```
/// assert_eq!(app::two(), 2);
/// ```
pub fn two() -> i32 { 2 }
#[cfg(test)] mod t {
    #[test] fn passes() {}
    #[test] fn fails() { panic!("broken"); }
    #[test] #[ignore] fn skipped() {}
}
''')
    (tmp_path/'tests').mkdir()
    (tmp_path/'tests/more.rs').write_text('#[test] fn other() { assert_eq!(app::two(), 2); }\n')
    (tmp_path/'.cargo').mkdir()
    (tmp_path/'.cargo/config.toml').write_text('[term]\nquiet = true\n')
    r = run_tests(tmp_path, '--no-fail-fast')
    assert not r.ok
    assert [(d.path, d.line, d.level) for d in r.diagnostics] == [('src/lib.rs', 1, 'warning')]
    unit, more, docs = r.suites
    assert [s.name for s in r.suites] == ['unittests src/lib.rs', 'tests/more.rs', 'Doc-tests app']
    assert unit.tests == {'t::passes': 'ok', 't::fails': 'FAILED', 't::skipped': 'ignored'} and unit.failed
    assert 'broken' in unit.failures['t::fails'] and list(unit.failures) == ['t::fails']
    assert more.tests == {'other': 'ok'} and not more.failed
    assert list(docs.tests.values()) == ['ok']
