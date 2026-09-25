"""Maintenance joins the ingest write lock, including lint's inherited lock."""
import fcntl
import os
from contextlib import contextmanager

from _batch_coordination import load_spine_reservation
from _progress import ProjectLock


@contextmanager
def maintenance_write_lock(config):
    inherited = os.environ.get('IMPROVED_WIKI_PROJECT_LOCK_FD')
    lock = None
    fd = None
    try:
        if inherited is not None:
            # Accept a real inherited descriptor, never an environment-only
            # assertion that another process happens to own a lock.
            fd = os.dup(int(inherited))
            actual = os.fstat(fd)
            expected = (config.runtime_dir / 'ingest.lock').stat()
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise RuntimeError('Inherited project lock belongs to another project')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            lock = ProjectLock(config, owner_id='maintenance')
            if not lock.acquire():
                raise RuntimeError('Project writer is active; maintenance refused')
        reservation = load_spine_reservation(config)
        if reservation:
            raise RuntimeError(
                'Ingest write spine is reserved by '
                f"{reservation.get('source_path', '?')}; resume or explicitly "
                'abandon it before maintenance')
        yield fd if fd is not None else lock._fd
    finally:
        if fd is not None:
            # Do not LOCK_UN a borrowed open-file description: lint retains it.
            os.close(fd)
        if lock is not None:
            lock.release()


def main(argv=None):
    """Exec a maintenance command with the same real, inherited project lock."""
    import argparse
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from _paths import detect_runtime_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("project")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.check and "IMPROVED_WIKI_PROJECT_LOCK_FD" not in os.environ:
            raise RuntimeError("No inherited project lock")
        config = SimpleNamespace(runtime_dir=detect_runtime_dir(Path(args.project)))
        with maintenance_write_lock(config) as fd:
            if args.check:
                return 0
            if not args.command:
                parser.error("a command is required")
            os.set_inheritable(fd, True)
            env = dict(os.environ, IMPROVED_WIKI_PROJECT_LOCK_FD=str(fd))
            # Shell children must use the interpreter that acquired the lock.
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
            os.execvpe(args.command[0], args.command, env)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"[maintenance] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
