import fcntl, os, subprocess, sys, pytest
from fastws.procs import JOB_ENV, kill_group


def test_nested_timeout_leaves_cleanup_to_job_owner(tmp_path):
    lock = tmp_path/'held'
    descendant = '''import fcntl, os, sys, time
f = open(sys.argv[1], 'a')
fcntl.flock(f, fcntl.LOCK_SH)
print('descendant group', os.getpgrp(), flush=True)
time.sleep(60)
'''
    child = f'''import os, subprocess, sys, time
print('child group', os.getpgrp(), os.environ.get('FASTWS_JOB'), flush=True)
subprocess.Popen([sys.executable, '-c', {descendant!r}, {str(lock)!r}])
time.sleep(60)
'''
    job = f'''import os, sys, time
from fastws.procs import bounded
print('job group', os.getpgrp(), flush=True)
try: bounded([sys.executable, '-c', {child!r}], .5, merge=True, env={{}})
except TimeoutError as e: print(e, flush=True)
print('job survived inner timeout', flush=True)
time.sleep(60)
'''
    with subprocess.Popen([sys.executable, '-c', job], start_new_session=True, env=os.environ | {JOB_ENV: '1'},
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as p:
        try:
            with pytest.raises(subprocess.TimeoutExpired): p.communicate(timeout=.8)
        finally: kill_group(p)
        output, _ = p.communicate(timeout=2)
    group = output.split('job group ')[1].split()[0]
    assert f'child group {group} 1' in output and f'descendant group {group}' in output
    assert 'job survived inner timeout' in output
    with lock.open() as f: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
