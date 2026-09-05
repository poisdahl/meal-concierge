"""Lifetime ownership of resolved household, browser and listener targets."""
from contextlib import ExitStack, contextmanager
import ctypes
import errno
import hashlib
import sys
import fcntl
import os
from pathlib import Path
import socket
import stat
import subprocess


@contextmanager
def effective_owner(uid=None, gid=None):
    # Service startup only, before Application creates any threads. Compose's
    # privileged core intentionally lacks DAC_OVERRIDE for the browser's files.
    old_uid, old_gid = os.geteuid(), os.getegid()
    try:
        if gid is not None and gid != old_gid:
            os.setegid(gid)
        if uid is not None and uid != old_uid:
            os.seteuid(uid)
        yield
    finally:
        if os.geteuid() != old_uid:
            os.seteuid(old_uid)
        if os.getegid() != old_gid:
            os.setegid(old_gid)


@contextmanager
def file_lock(path, uid=None, gid=None):
    path = Path(path)
    with effective_owner(uid, gid):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError('ownership lock is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f'target already owned: {path}') from exc
        yield
    finally:
        os.close(fd)


def process_arguments(pid):
    if sys.platform.startswith('linux'):
        base = Path('/proc') / str(pid)
        return [v.decode() for v in (base / 'cmdline').read_bytes().split(b'\0') if v], base / 'cwd'
    # macOS ps flattens argv and loses spaces. KERN_PROCARGS2 retains exact argv.
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, int(pid))
    size = ctypes.c_size_t(1024 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), 'cannot inspect legacy process argv')
    data = buffer.raw[:size.value]
    argc = int.from_bytes(data[:4], sys.byteorder)
    offset = data.index(b'\0', 4)
    while offset < len(data) and data[offset] == 0:
        offset += 1
    words = [v.decode() for v in data[offset:].split(b'\0')[:argc]]
    cwd = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], check=True, text=True, capture_output=True)
    paths = [v[1:] for v in cwd.stdout.splitlines() if v.startswith('n')]
    if len(paths) != 1:
        raise RuntimeError('cannot determine legacy service working directory')
    return words, Path(paths[0])


def assert_no_legacy_service(state, profile, browser_home=None, browser_socket_directory=None, cdp=None, *, resolved_targets=None, browser_uid=None, browser_gid=None):
    """Old services have no lock. Check exact argv/cwd before any data initialization."""
    targets = resolved_targets if resolved_targets is not None else {str(Path(p).resolve()) for p in (state, profile, browser_home, browser_socket_directory) if p}
    result = subprocess.run(['ps', '-axo', 'pid=,command='], check=True, text=True, capture_output=True)
    for line in result.stdout.splitlines():
        pid, _, command = line.strip().partition(' ')
        if pid == str(os.getpid()) or ('service.py' not in command and '-m service' not in command) or '--state' not in command or '--config' not in command:
            continue
        try:
            words, cwd = process_arguments(pid)
        except (ProcessLookupError, FileNotFoundError):
            continue
        if not any(Path(w).name == 'service.py' for w in words) and not any(words[i:i+2] == ['-m', 'service'] for i in range(len(words))):
            continue
        if not all(any(w == flag or w.startswith(flag + '=') for w in words) for flag in ('--state', '--config')):
            continue
        if not any(w == '--browser-profile' or w.startswith('--browser-profile=') for w in words):
            raise RuntimeError(f'legacy service PID {pid} has an implicit browser profile; stop it before takeover')
        for index, word in enumerate(words):
            for flag in ('--state', '--browser-profile', '--browser-home', '--browser-socket-directory', '--browser-cdp'):
                value = words[index + 1] if word == flag and index + 1 < len(words) else word.removeprefix(flag + '=') if word.startswith(flag + '=') else None
                if value:
                    if flag == '--browser-cdp':
                        overlaps = value == cdp
                    else:
                        uid, gid = (browser_uid, browser_gid) if flag.startswith('--browser') else (None, None)
                        with effective_owner(uid, gid):
                            resolved = (Path(value) if Path(value).is_absolute() else cwd.resolve(strict=True) / value).resolve()
                        overlaps = str(resolved) in targets
                    if overlaps:
                        raise RuntimeError(f'existing service PID {pid} owns this target; stop its exact supervisor before takeover')


@contextmanager
def ownership(state, profile, browser_home=None, browser_socket_directory=None, cdp=None, browser_uid=None, browser_gid=None):
    state = Path(state).resolve()
    with effective_owner(browser_uid, browser_gid):
        profile = Path(profile).resolve()
        browser_paths = {Path(p).resolve() / '.service-owner.lock' for p in (profile, browser_home, browser_socket_directory) if p}
    # A shared/symlinked JSON or database must not evade the directory lock.
    paths = {state / '.service-owner.lock'}
    for name in ('state.json', 'recipes.sqlite3'):
        target = (state / name).resolve()
        paths.add(target.parent / (target.name + '.owner.lock'))
    if cdp:
        digest = hashlib.sha256(cdp.encode()).hexdigest()
        paths.add(Path('/tmp') / f'meal-concierge-owner-{os.getuid()}' / (digest + '.lock'))
    with ExitStack() as stack:
        for path in sorted(paths):
            stack.enter_context(file_lock(path))
        for path in sorted(browser_paths):
            stack.enter_context(file_lock(path, browser_uid, browser_gid))
        assert_no_legacy_service(state, profile, browser_home, browser_socket_directory, cdp,
                                 resolved_targets={str(state), *(str(p.parent) for p in browser_paths)},
                                 browser_uid=browser_uid, browser_gid=browser_gid)
        yield


@contextmanager
def listener_ownership(path):
    path = Path(path).resolve()
    with file_lock(path.parent / (path.name + '.owner.lock')):
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise RuntimeError('refusing to replace a non-socket listener path')
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(path))
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise RuntimeError('existing listener cannot be proven stopped') from exc
                else:
                    raise RuntimeError('existing listener is active; refusing takeover')
            path.unlink()
        yield
