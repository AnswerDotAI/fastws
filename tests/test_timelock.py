import os, threading, pytest
from fastws import timelock
if os.name != 'nt': import fcntl

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Windows has no timing lock')

def test_shared_hold_outlives_other_thread(tmp_path, monkeypatch):
    "One thread's release leaves the other thread's shared hold in place."
    monkeypatch.setattr(timelock, 'LOCK', str(tmp_path/'lock'))
    monkeypatch.setattr(timelock, 'GATE', str(tmp_path/'gate'))
    held, release = threading.Event(), threading.Event()
    def hold():
        with timelock.timing_lock(): held.set(); assert release.wait(3)
    t = threading.Thread(target=hold, daemon=True)
    try:
        with timelock.timing_lock():
            t.start(); assert held.wait(3)
        with open(timelock.LOCK) as f, pytest.raises(BlockingIOError): fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()
        if t.ident is not None: t.join(3)
    assert not t.is_alive()

def test_holds_read_only_lock_files(tmp_path, monkeypatch):
    "Lock files that can't be opened for append, as another user's can't in `/tmp`, are still locked."
    for name in ('LOCK', 'GATE'):
        monkeypatch.setattr(timelock, name, str(tmp_path/name))
        (tmp_path/name).touch(0o444)
    for exclusive in (False, True):
        with timelock.timing_lock(exclusive), open(timelock.LOCK) as f, pytest.raises(BlockingIOError): fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
