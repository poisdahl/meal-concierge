"""Undo a stopped update only with affirmative, durable pre-use evidence."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from core import _atomic_json, _flush_drive_cache
from runtime_ownership import file_lock

GUARD = ".release-use.json"
# These lock files contain no payload and must retain their original inodes.
LOCKS = {".service-owner.lock", ".release-use.lock", "state.json.owner.lock", "recipes.sqlite3.owner.lock", "state.lock"}


def note_runtime_open(state):
    """Called before StateStore can create, reconcile, or operate on a household."""
    path = Path(state) / GUARD
    try:
        info = path.lstat()
    except FileNotFoundError:
        return  # Pre-protocol, or never managed by the installer.
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("invalid release lifecycle guard")
    with file_lock(path.with_name('.release-use.lock')):
        value = json.loads(path.read_text())
        if value.get("status") == "rolling_back":
            raise RuntimeError("offline rollback is in progress")
        if value.get("status") not in {"never_started", "possibly_used"}:
            raise RuntimeError("release lifecycle evidence is unavailable")
        if value["status"] == "never_started":
            _atomic_json(path, {**value, "status": "possibly_used"})


def content_hash(path):
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_files(root):
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("rollback requires an owned regular data directory")
    result = {}
    for parent, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(parent) / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("rollback data contains a linked or irregular entry")
            relative = path.relative_to(root).as_posix()
            if relative not in LOCKS and relative != GUARD:
                result[relative] = content_hash(path)
    return result


def old_identity(release):
    from build_identity import create, verify_release_layout
    verify_release_layout(release)
    value = create(release, release, "source-and-installed-bytes")
    return [value["source_sha256"], value["installed_runtime_sha256"]]


def state_fingerprint(state):
    return sha256(json.dumps(payload_files(state), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def preimage_fingerprint(backup):
    material = [payload_files(backup / 'state'), content_hash(backup / 'config.json'),
                content_hash(backup / 'backup.json')]
    return sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def arm(home, meta, previous, backup, release, operation, previous_identity):
    state = Path(meta["paths"]["state"])
    prior = Path(previous["release"])
    if not prior.is_dir() or prior.is_symlink():
        return  # No unambiguous previous release to restore.
    original = payload_files(backup / "state")
    current = payload_files(state)
    guard = {"release": release.name, "operation": operation, "status": "never_started"}
    receipt_path = home / 'rollback.json'
    if receipt_path.exists():
        from install import bounded_json
        existing = bounded_json(receipt_path, 32 * 1024 * 1024)
        if existing.get('guard', {}).get('operation') == operation:
            # A retry must never reset evidence of use. If a crash split
            # receipt and guard publication, an incomplete guard stays unknown.
            return
    if old_identity(prior) != previous_identity:
        raise RuntimeError('previous runtime changed during upgrade; recover forward')
    cfg = content_hash(backup / "config.json")
    if cfg != content_hash(Path(meta["paths"]["config"])):
        raise RuntimeError("configuration changed during the offline upgrade")
    # Sync the reviewed pre-image before making any claim of recoverability.
    for name in [*original, "../config.json", "../backup.json"]:
        with (backup / "state" / name).open("rb") as f:
            os.fsync(f.fileno())
    receipt = {"format": 1, "status": "armed", "previous_manifest": previous,
               "release": str(release), "backup": str(backup), "guard": guard,
               "before": original, "after": current, "config_hash": cfg, "previous_identity": previous_identity}
    with file_lock(state / ".release-use.lock"):
        _atomic_json(home / "rollback.json", receipt)
        _atomic_json(state / GUARD, guard)


def staged_path(destination, operation):
    return destination.with_name('.rollback-' + operation + '-' + destination.name)


def check_partial(source, temporary):
    """Only an exact prefix of the reviewed pre-image can be resumed."""
    fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as part, source.open('rb') as original:
        info = os.fstat(part.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
            raise RuntimeError('rollback staging identity changed')
        for chunk in iter(lambda: part.read(1024 * 1024), b''):
            if original.read(len(chunk)) != chunk:
                raise RuntimeError('rollback staging contains unrelated bytes; preserve for recovery')
        return info


def restore_file(source, destination, expected, operation):
    tmp = staged_path(destination, operation)
    if tmp.exists() or tmp.is_symlink():
        checked = check_partial(source, tmp)
        fd = os.open(tmp, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (checked.st_dev, checked.st_ino):
            os.close(fd)
            raise RuntimeError('rollback staging changed before resuming')
    else:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as dest, source.open("rb") as src:
        offset = os.lseek(dest.fileno(), 0, os.SEEK_END)
        src.seek(offset)
        shutil.copyfileobj(src, dest)
        dest.flush()
        os.fsync(dest.fileno())
        _flush_drive_cache(dest.fileno())
    if content_hash(tmp) != expected:
        raise RuntimeError("backup changed during rollback")
    os.replace(tmp, destination)
    parent = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def rollback(home, meta):
    from install import offline, bounded_json, write_unit
    with offline(meta), file_lock(Path(meta["paths"]["state"]) / ".release-use.lock"):
        if not (home / "rollback.json").is_file():
            raise RuntimeError("no pre-use rollback evidence; preserve state and recover forward")
        record = bounded_json(home / "rollback.json", 32 * 1024 * 1024)
        if record.get("format") != 1 or record.get("status") not in {"armed", "rolling_back", "finishing", "completed"}:
            raise RuntimeError("no pre-use rollback evidence; preserve state and recover forward")
        old, guard = record["previous_manifest"], record["guard"]
        if record["status"] in {"finishing", "completed"}:
            marker = home / "maintenance.json"
            if meta != old or (Path(meta["code_root"]) / "current").resolve() != Path(old["release"]).resolve():
                raise RuntimeError("rollback completion belongs to a different installation")
            if marker.exists():
                if bounded_json(marker) != {"kind": "rollback", "operation": guard["operation"]}:
                    raise RuntimeError("another maintenance action needs recovery")
                _atomic_json(Path(meta["paths"]["state"]) / GUARD, {**guard, "status": "possibly_used"})
                _atomic_json(home / "rollback.json", {**record, "status": "completed"})
                marker.unlink()
            return {"status": "rolled_back", "service_started": False}
        resuming = record["status"] == "rolling_back"
        if ((meta["release"] != record["release"] and not (resuming and meta == old))
                or old["paths"]["state"] != meta["paths"]["state"] or old["code_root"] != meta["code_root"]):
            raise RuntimeError("rollback belongs to a different installation or release")
        code = Path(meta["code_root"]).resolve()
        prior = Path(old["release"])
        backup = Path(record["backup"])
        if (prior.parent.resolve() != code or prior.is_symlink() or not prior.is_dir()
                or not re.fullmatch(r"release-[0-9a-f]{32}", prior.name)
                or backup.parent.resolve() != (home / "backups").resolve() or backup.is_symlink()
                or not re.fullmatch(r"before-[0-9a-f]{32}", backup.name)):
            raise RuntimeError("previous release or backup is unavailable")
        allowed_targets = {Path(record["release"]).resolve()}
        if resuming:
            allowed_targets.add(prior.resolve())
        if (code / "current").resolve() not in allowed_targets or old_identity(prior) != record["previous_identity"]:
            raise RuntimeError("selected release or previous program bytes changed")
        state = Path(meta["paths"]["state"])
        if meta.get("hosted_oda_plugin"):
            from hosted_oda_cart import require_idle

            require_idle(state, target_program=prior)
        evidence = bounded_json(state / GUARD)
        if evidence != guard and not (resuming and evidence == {**guard, "status": "rolling_back"}):
            raise RuntimeError("release may have started; preserve all journals and recover forward")
        before, after = record["before"], record["after"]
        if (payload_files(backup / "state") != before or content_hash(backup / "config.json") != record["config_hash"]
                or content_hash(Path(meta["paths"]["config"])) != record["config_hash"]):
            raise RuntimeError("backup or configuration changed; recover forward")
        current = payload_files(state)
        # A migration may create immutable schema backups. Keep them. Only the
        # two authoritative files migrated by install.migrate_owned may be reverted.
        mutable = {"state.json", "recipes.sqlite3"}
        if resuming:
            for name in mutable:
                temporary = staged_path(state / name, guard["operation"])
                if name in before and after.get(name) != before[name] and temporary.name in current:
                    check_partial(backup / "state" / name, temporary)
                    current.pop(temporary.name)
        if (set(current).difference(after) or any(current.get(k) != v and not (
                resuming and k in mutable and current.get(k) == before.get(k)) for k, v in after.items())
                or any(k not in after or before[k] != after[k] and k not in mutable for k in before)):
            raise RuntimeError("household changed after the update; preserve later work and recover forward")
        if not (home / "maintenance.json").exists():
            _atomic_json(home / "maintenance.json", {"kind": "rollback", "operation": guard["operation"]})
        elif bounded_json(home / "maintenance.json") != {"kind": "rollback", "operation": guard["operation"]}:
            raise RuntimeError("another maintenance action needs recovery")
        _atomic_json(home / "rollback.json", {**record, "status": "rolling_back"})
        _atomic_json(state / GUARD, {**guard, "status": "rolling_back"})
        for name in mutable:
            if name in before and current.get(name) != before[name]:
                restore_file(backup / "state" / name, state / name, before[name], guard["operation"])
            elif name in current and name not in before:
                # Keep an unused newly created database in the recovery backup,
                # then remove the empty installation from the prior schema path.
                saved = backup / ("unused-" + name)
                restore_file(state / name, saved, after[name], guard["operation"])
                (state / name).unlink()
        write_unit(old)
        link = code / (".current-" + uuid.uuid4().hex)
        link.symlink_to(prior.name)
        os.replace(link, code / "current")
        _atomic_json(home / "runtime.json", old)
        _atomic_json(home / "rollback.json", {**record, "status": "finishing"})
        _atomic_json(state / GUARD, {**guard, "status": "possibly_used"})
        _atomic_json(home / "rollback.json", {**record, "status": "completed"})
        (home / "maintenance.json").unlink()
        return {"status": "rolled_back", "service_started": False}
