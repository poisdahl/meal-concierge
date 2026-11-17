"""Review and redact credential query parameters in a private export copy."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import parse_qsl, unquote_plus, urlsplit, urlunsplit

from recipe_import_readers import _CREDENTIAL_KEYS
from recipe_portable import MAX_RECIPE_BYTES, _regular_file, canonical_bytes, export_private_archive
from recipes import RecipeError, RecipeStore, source_key

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _clean_url(match):
    original = match.group(0)
    parts = urlsplit(original)
    def clean(value):
        if not any(k.casefold() in _CREDENTIAL_KEYS for k, _ in parse_qsl(value, keep_blank_values=True)):
            return value
        return "&".join(piece for piece in value.split("&")
                        if unquote_plus(piece.partition("=")[0]).casefold() not in _CREDENTIAL_KEYS)
    query, fragment = clean(parts.query), clean(parts.fragment)
    authority = parts.netloc.rsplit("@", 1)[-1]
    if (authority, query, fragment) == (parts.netloc, parts.query, parts.fragment):
        return original
    return urlunsplit((parts.scheme, authority, parts.path, query, fragment))


def _transform(value):
    if isinstance(value, str):
        return _URL.sub(_clean_url, value)
    if isinstance(value, dict):
        return {k: _transform(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_transform(v) for v in value]
    return value


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _inventory(db, *, apply=False):
    details = []
    for row in db.execute("""SELECT v.recipe_id,v.revision,v.document,r.revision
                           FROM revisions v JOIN recipes r ON r.id=v.recipe_id ORDER BY v.recipe_id,v.revision"""):
        rid, revision, raw, head = row
        if len(raw) > MAX_RECIPE_BYTES:
            raise RecipeError("stored recipe revision exceeds the review bound")
        original = json.loads(raw)
        changed = _transform(original)
        if changed == original:
            continue
        identity = rid if re.fullmatch(r"rec_[0-9a-f]{24}|[0-9a-f-]{32,36}", rid) else "sha256:" + _digest(rid.encode())
        bindings = {}
        if "translations" in original:
            from recipe_languages import normalize_translations, source_text_digest
            # A redacted derivative can rebind reviewed text only when every
            # prior translation was valid for the exact canonical pre-image.
            normalize_translations(original["translations"], original)
            modern_binding = source_text_digest(original)
            for language, variant in changed["translations"].items():
                old_binding = original["translations"][language]["source_text_digest"]
                new_binding = source_text_digest(changed, legacy=old_binding != modern_binding)
                variant["source_text_digest"] = new_binding
                bindings[language] = {"original": old_binding, "redacted": new_binding}
            normalize_translations(changed["translations"], changed)
        details.append({"recipe_id": identity, "translation_bindings": bindings, "revision": revision, "current_revision": head,
                        "original_sha256": _digest(canonical_bytes(original)), "redacted_sha256": _digest(canonical_bytes(changed))})
        if apply:
            text = canonical_bytes(changed).decode()
            db.execute("UPDATE revisions SET document=? WHERE recipe_id=? AND revision=?", (text, rid, revision))
            if revision == head:
                db.execute("UPDATE recipes SET document=?,source_key=CASE WHEN source_key IS ? THEN ? ELSE source_key END WHERE id=?",
                           (text, source_key(original), source_key(changed), rid))
    return details


def review(store):
    for ext in ("-wal", "-journal"):
        side = Path(str(store.path) + ext)
        if side.is_symlink() or side.exists() and side.stat().st_size:
            raise RecipeError("private URL review requires an idle rollback-journal bank")
    digest = hashlib.sha256()
    with _regular_file(store.path) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    with closing(sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("SELECT value FROM metadata WHERE key='household'").fetchone() != (store.household,):
            raise RecipeError("private URL review household differs")
        details = _inventory(db)
    bank = digest.hexdigest()
    return {"review": _digest(canonical_bytes([bank, details])), "bank_sha256": bank,
            "affected_revisions": len(details), "items": details[:100], "truncated": len(details) > 100}


def export_reviewed(store, destination, expected, backup):
    receipt = review(store)
    if expected != receipt["review"] or not receipt["affected_revisions"]:
        raise RecipeError("private URL review changed or has no remediation candidates")
    destination, backup = Path(destination), Path(backup)
    if destination.exists() or destination.is_symlink() or backup == destination:
        raise RecipeError("private redacted export requires separate unused backup and archive destinations")
    with _regular_file(store.path) as source:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
    parent = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    class ExportCopy(RecipeStore):
        @property
        def assets(self):
            return store.assets
    with tempfile.TemporaryDirectory(prefix=".redacted-export-", dir=destination.parent) as tmp:
        copy = ExportCopy(Path(tmp) / "recipes.sqlite3", store.household)
        shutil.copyfile(backup, copy.path)
        if review(copy)["review"] != expected:
            raise RecipeError("private backup differs from the reviewed revisions")
        with closing(sqlite3.connect(copy.path)) as db, db:
            details = _inventory(db, apply=True)
        archive = Path(tmp) / "redacted.zip"
        manifest = export_private_archive(archive, copy)
        digest = hashlib.sha256()
        with archive.open("rb") as checked:
            for block in iter(lambda: checked.read(1024 * 1024), b""):
                digest.update(block)
        audit = {"format": 1, "review": expected, "bank_sha256": receipt["bank_sha256"],
                 "archive_sha256": digest.hexdigest(), "revisions": details}
        audit_bytes = canonical_bytes(audit)
        audit_path = Path(str(destination) + ".audit.json")
        with (Path(tmp) / "audit").open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(audit_bytes)
            output.flush()
            os.fsync(output.fileno())
        # No existing archive or audit can be overwritten. The audit is durable
        # before the redacted copy is published.
        parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.link(Path(tmp) / "audit", audit_path)
            os.fsync(parent)
            os.link(archive, destination)
            os.fsync(parent)
        finally:
            os.close(parent)
    return {**receipt, "exported": True, "backup_preserved": True, "recipes_count": manifest["recipes_count"],
            "audit_sha256": _digest(audit_bytes)}
