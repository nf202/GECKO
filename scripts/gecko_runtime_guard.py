"""Linux-only process locks for project-owned Gecko startup and Java cache."""
from contextlib import contextmanager
from pathlib import Path
# Keep reference-only imports portable; campaign locks require Linux.

@contextmanager
def _lock(path):
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError('Database campaigns require Linux (fcntl locks)') from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)

def startup_lock(root):
    return _lock(Path(root)/'runtime/gecko-startup.lock')

def cache_lock(root, tag):
    return _lock(Path(root)/'runtime/gremlin-runner'/('compile-'+tag+'.lock'))
