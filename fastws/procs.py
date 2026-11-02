r"Bounded commands, with one process-group owner for each Unix job."
import os, signal, subprocess
from contextlib import suppress
from pathlib import Path

__all__ = ['bounded', 'kill_group', 'JOB_ENV']
JOB_ENV = 'FASTWS_JOB' # Owners mark the environment of their job; subordinate commands inherit its process group


def kill_group(p):
    "Kill the process group that `p` owns, then reap its leader, even if the leader already exited. Only use for a process launched with `start_new_session=True`."
    with suppress(ProcessLookupError): os.killpg(p.pid, signal.SIGKILL)
    p.wait()


def bounded(
    cmd, # The command and its arguments, as `subprocess.Popen` takes them
    seconds:float=None, # Seconds that it may run; None: no limit
    input:str=None, # Text written to its standard input
    merge:bool=False, # Merge standard error into standard output, in the order written?
    **kwargs # Other `subprocess.Popen` arguments, such as `cwd` and `env`
)->subprocess.CompletedProcess:
    "Run a command with captured text and a wall-clock limit. Without `FASTWS_JOB`, own a new session and kill its process group on completion, timeout or error. Within a marked job, inherit its group; a timeout/error kills only the direct child and raises. Let that error end the job so its owner cleans up descendants, even after its leader exits. Environment overrides retain the marker. Commands must not detach from the job's group."
    owner = not os.environ.get(JOB_ENV)
    env = kwargs.pop('env', None)
    env = dict(os.environ if env is None else env) | {JOB_ENV: '1'}
    with subprocess.Popen(cmd, stdin=None if input is None else subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if merge else subprocess.PIPE, text=True, start_new_session=owner, env=env, **kwargs) as p:
        try:
            try: out, err = p.communicate(input, timeout=seconds)
            except BaseException as e:
                if owner:
                    kill_group(p)
                    out, err = p.communicate()
                else:
                    p.kill()
                    p.wait()
                    out, err = (s.decode(errors='replace') if isinstance(s, bytes) else s or ''
                                for s in (getattr(e, 'output', ''), getattr(e, 'stderr', '')))
                if not isinstance(e, subprocess.TimeoutExpired): raise
                tail = '\n'.join(f'    {line}' for line in (out + (err or '')).splitlines()[-20:])
                cleanup = 'its process group was killed' if owner else 'its child was killed; the job owner must clean up descendants'
                raise TimeoutError(f"`{Path(str(cmd[0])).name}` didn't finish in {seconds:.3g} s: {cleanup}. Pass a larger `timeout`, or run less at once." + (f" Its last lines:\n{tail}" if tail else '')) from None
        finally:
            if owner: kill_group(p)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)
