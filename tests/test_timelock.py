import os, threading, pytest
from fastws import timelock
if os.name != 'nt': import fcntl

@pytest.mark.skipif(os.name == 'nt', reason='Windows has no timing lock')
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
