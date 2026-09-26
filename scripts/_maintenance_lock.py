"""Maintenance joins the ingest write lock, including lint's inherited lock.

Lint runs additionally hold ``<runtime>/lint.lock`` so two lint runs never
share lint state. A mutation-free lint (``--read-only``) holds only that lock:
it reads the wiki and writes lint state, so it neither waits for nor blocks an
ingest.
"""
import fcntl
import os
from contextlib import contextmanager

from _batch_coordination import load_spine_reservation
from _progress import ProjectLock

PROJECT_LOCK_ENV = 'IMPROVED_WIKI_PROJECT_LOCK_FD'
LINT_LOCK_ENV = 'IMPROVED_WIKI_LINT_LOCK_FD'


class MaintenanceLockError(RuntimeError):
    """Maintenance could not join the project write lock."""


def _borrow(env_name: str, lock_path, label: str) -> int:
    """Duplicate and re-lock a real inherited descriptor for ``lock_path``.

    Never trust an environment-only assertion that another process happens
    to own a lock: the descriptor must be the same file and still lockable.
    """
    fd = os.dup(int(os.environ[env_name]))
    try:
        actual = os.fstat(fd)
        expected = lock_path.stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise MaintenanceLockError(f'Inherited {label} belongs to another project')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def maintenance_write_lock(config):
    inherited = os.environ.get(PROJECT_LOCK_ENV)
    lock = None
    fd = None
    try:
        if inherited is not None:
            fd = _borrow(PROJECT_LOCK_ENV, config.runtime_dir / 'ingest.lock',
                         'project lock')
        else:
            lock = ProjectLock(config, owner_id='maintenance')
            if not lock.acquire():
                raise MaintenanceLockError('Project writer is active; maintenance refused')
        reservation = load_spine_reservation(config)
        if reservation:
            raise MaintenanceLockError(
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


def _hold_lint_lock(runtime_dir) -> int:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(runtime_dir / 'lint.lock', os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise MaintenanceLockError('Another lint run is active in this project')
    return fd


def main(argv=None):
    """Exec a maintenance command with the same real, inherited project lock."""
    import argparse
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    from _paths import detect_runtime_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify the inherited locks instead of running a command")
    parser.add_argument("--lint", action="store_true",
                        help="also hold the project's lint-run lock")
    parser.add_argument("--read-only", action="store_true",
                        help="hold only the lint-run lock (mutation-free lint)")
    parser.add_argument("project")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    lint = args.lint or args.read_only
    try:
        config = SimpleNamespace(runtime_dir=detect_runtime_dir(Path(args.project)))
        if args.check:
            for env_name, needed, path, label in (
                    (PROJECT_LOCK_ENV, not args.read_only,
                     config.runtime_dir / 'ingest.lock', 'project lock'),
                    (LINT_LOCK_ENV, lint, config.runtime_dir / 'lint.lock', 'lint lock')):
                if not needed:
                    continue
                if env_name not in os.environ:
                    raise MaintenanceLockError(f"No inherited {label}")
                os.close(_borrow(env_name, path, label))
            if not args.read_only:
                with maintenance_write_lock(config):
                    pass
            return 0
        if not args.command:
            parser.error("a command is required")
        env = dict(os.environ)
        env.pop(PROJECT_LOCK_ENV, None)
        env.pop(LINT_LOCK_ENV, None)
        if lint:
            lint_fd = _hold_lint_lock(config.runtime_dir)
            os.set_inheritable(lint_fd, True)
            env[LINT_LOCK_ENV] = str(lint_fd)
        # Shell children must use the interpreter that acquired the lock.
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        if args.read_only:
            reservation = load_spine_reservation(config)
            if reservation:
                print("[maintenance] read-only lint while the ingest write spine is "
                      f"reserved by {reservation.get('source_path', '?')}; findings "
                      "may reflect a partial write", file=sys.stderr)
            os.execvpe(args.command[0], args.command, env)
        with maintenance_write_lock(config) as fd:
            os.set_inheritable(fd, True)
            env[PROJECT_LOCK_ENV] = str(fd)
            os.execvpe(args.command[0], args.command, env)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"[maintenance] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
