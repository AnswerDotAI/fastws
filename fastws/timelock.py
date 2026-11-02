r"The machine's timing lock, which keeps timings apart from builds and test runs, as `rustymath::timing` describes: a timing locks `LOCK` exclusively, holding `GATE` exclusively from the start of its wait, and anything else passes `GATE` and then locks `LOCK` shared. It imports only the standard library, because the rustc wrapper that `ws-sync` writes loads it for every rustc call."
import os, sys, threading, time
from contextlib import contextmanager
if os.name != 'nt': import fcntl

LOCK = '/tmp/aai-ws-timing.lock' # Timings lock it exclusively; builds and test runs lock it shared
GATE = '/tmp/aai-ws-timing.gate' # A timing holds it exclusively while it waits for `LOCK` and holds it; a shared hold locks it for a moment first
_holds = threading.local() # This thread's hold mode, as `exclusive`; nested shared holds reuse it


def _users():
    "The other processes that have `LOCK` open, as `pid command` strings from `lsof`. On macOS, where `lsof` doesn't show `flock` holds, the list also includes any process waiting for the lock."
    import subprocess
    users, pid = [], ''
    for line in subprocess.run(['lsof', '+c', '0', '-F', 'pc', LOCK], capture_output=True, text=True).stdout.splitlines():
        if line[:1] == 'p': pid = line[1:]
        elif line[:1] == 'c' and pid != str(os.getpid()): users.append(f'{pid} {line[1:]}')
    return users


class _Wait:
    "One hold's wait for `GATE` and then `LOCK`, against one deadline of 5 minutes."
    def __init__(self, exclusive, quiet): self.exclusive, self.quiet, self.start, self.waiting, self.reported = exclusive, quiet, time.monotonic(), False, False

    def take(self, f):
        "Lock `f` as the hold needs it, waiting up to 5 minutes before raising `TimeoutError`. An exclusive hold that has waited 30 seconds prints, once, the other processes that have `LOCK` open."
        others = 'a build or another timing' if self.exclusive else 'a timing'
        while True:
            try: return fcntl.flock(f, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError: pass
            waited = time.monotonic() - self.start
            if waited > 300: raise TimeoutError(f'{others} has held {LOCK} for 5 minutes')
            if not self.waiting and not self.quiet: print(f'waiting for {others} to release {LOCK}', file=sys.stderr)
            if self.exclusive and not self.reported and waited > 30:
                print(f'{LOCK} is still taken after 30 seconds. Processes with it open: {", ".join(_users()) or "none"}', file=sys.stderr)
                self.reported = True
            self.waiting = True
            time.sleep(0.25)


@contextmanager
def _held(exclusive, quiet=False):
    "Hold `LOCK`, and `GATE` too when exclusive. `quiet` leaves out the message that the wait has started."
    with open(GATE, 'a') as gate, open(LOCK, 'a') as lock:
        wait = _Wait(exclusive, quiet)
        wait.take(gate)
        wait.take(lock)
        if not exclusive: gate.close()
        yield lock


@contextmanager
def timing_lock(
    exclusive=False, # Take the lock exclusively, as a timing does; False: take it shared, as a build or a test run does
):
    "Hold the Unix lock that keeps this machine's timings apart from participating builds and test runs; a no-op on Windows. Acquisition raises `TimeoutError` after 5 minutes. Shared-inside-shared in the same thread reuses its hold. Any nesting involving exclusive raises `RuntimeError`. Each thread takes its own shared lock; one thread's release never ends another's hold."
    if os.name == 'nt':
        yield
        return
    held = getattr(_holds, 'exclusive', None)
    if held is not None:
        if held or exclusive: raise RuntimeError('timing locks can only nest shared inside shared')
        yield
        return
    with _held(exclusive):
        _holds.exclusive = exclusive
        try: yield
        finally: del _holds.exclusive


def exec_shared(argv):
    "Replace this process with `argv`, which then holds the lock shared until it ends, as the rustc wrapper does for each rustc call. It prints nothing while it waits, since Cargo shows each rustc call's output."
    if os.name == 'nt': os.execv(argv[0], argv)
    with _held(False, quiet=True) as lock:
        os.set_inheritable(lock.fileno(), True)
        os.execv(argv[0], argv)
