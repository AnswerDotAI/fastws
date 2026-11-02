import hashlib, os, time, pytest
from fastws import store

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Unix store locks and symbolic-link installation')


def test_capture_install_and_collect(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'STORE', tmp_path/'store')
    root = tmp_path/'project'
    root.mkdir()
    (root/'pyproject.toml').write_text('[project]\nname = "example"\n')
    source, artifact, installed = root/'source', root/'artifact', root/'installed'
    with store.building(root) as started, store.access():
        source.write_text('new input')
        artifact.write_bytes(b'first')
        first = store.keep(root, {'bin/tool': artifact, 'python/data': artifact}, [source], started)
        assert store.keep(root, {'bin/tool': artifact, 'python/data': artifact}, [source], started) == first
        store.install(root, first, {installed: 'bin/tool'})
    assert installed.read_bytes() == b'first' and store.resolve(root) == first
    assert (first/'bin/tool').stat().st_ino == (first/'python/data').stat().st_ino
    assert store.builds(root)[0]['edited'] == [str(source)]
    kept = []
    with store.building(root) as started, store.access():
        for i in range(store.KEEP+1):
            artifact.write_text(str(i))
            kept.append(store.keep(root, {'bin/tool': artifact}, [source], started))
        old = time.time() - (store.KEEP_DAYS+1)*86400
        for i, path in enumerate([first, *kept]): os.utime(path/'build.json', (old+i, old+i))
        for blob in (store.STORE/'blobs').iterdir(): os.utime(blob, (old, old))
    store.collect()
    assert first.exists() and kept[-1].exists() and not kept[0].exists()
    assert installed.read_bytes() == b'first'
    assert not (store.STORE/'blobs'/hashlib.sha256(b'0').hexdigest()).exists()
