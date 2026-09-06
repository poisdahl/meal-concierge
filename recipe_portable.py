"""Versioned, bounded recipe-pack framing shared by the builder and importer.

This module grants no source trust and does not mutate the recipe bank. A caller
must select the trusted installation/private restore path outside the archive.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
from typing import Any, BinaryIO, Iterator, Mapping
import zipfile
import zlib

from recipes import MAX_IMPORT_RECORDS, MAX_RECIPE_BYTES, RecipeError


FORMAT = "meal-concierge-recipes"
FORMAT_VERSION = 1
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * MAX_ARCHIVE_BYTES
MAX_RECORDS_BYTES = 512 * 1024 * 1024
MAX_RECORD_BYTES = 2 * MAX_RECIPE_BYTES
MAX_ASSET_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = MAX_IMPORT_RECORDS + 4
CHUNK_BYTES = 128 * 1024
_ASSET = re.compile(r"assets/([0-9a-f]{64})\.jpg\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise RecipeError("portable recipe JSON is invalid") from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecipeError("portable JSON contains duplicate keys")
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RecipeError("portable recipe JSON is invalid") from exc


def _limit(name: str) -> int:
    if name == "manifest.json":
        return MAX_MANIFEST_BYTES
    if name == "records.jsonl":
        return MAX_RECORDS_BYTES
    if name in {"attribution.json", "coverage.json"}:
        return MAX_REPORT_BYTES
    if _ASSET.fullmatch(name):
        return MAX_ASSET_BYTES
    raise RecipeError("portable archive has an unsupported member path")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(
        ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value
    ):
        raise RecipeError(f"portable {field} is invalid")
    return value


def _manifest(value: Any) -> dict:
    if not isinstance(value, dict) or value.get("format") != FORMAT or type(value.get("format_version")) is not int or value["format_version"] != FORMAT_VERSION:
        raise RecipeError("unsupported portable recipe format")
    if value.get("kind") != "bundled":
        raise RecipeError("portable private restore format is not yet supported")
    for field in ("pack_id", "pack_version", "normalizer_version"):
        _text(value.get(field), field)
    if type(value.get("recipe_schema_version")) is not int or value["recipe_schema_version"] not in {1, 2}:
        raise RecipeError("unsupported portable recipe schema")
    count = value.get("records_count")
    if type(count) is not int or not 0 <= count <= MAX_IMPORT_RECORDS:
        raise RecipeError("portable recipe count is invalid")
    files = value.get("files")
    if not isinstance(files, list) or not 1 <= len(files) < MAX_MEMBERS:
        raise RecipeError("portable file inventory is invalid")
    seen = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise RecipeError("portable file inventory is invalid")
        name = item["path"]
        if not isinstance(name, str) or name == "manifest.json" or name in seen:
            raise RecipeError("portable file inventory has duplicate/invalid paths")
        maximum = _limit(name)
        if type(item["bytes"]) is not int or not 0 <= item["bytes"] <= maximum:
            raise RecipeError("portable file size is invalid")
        if not isinstance(item["sha256"], str) or not _DIGEST.fullmatch(item["sha256"]):
            raise RecipeError("portable file digest is invalid")
        match = _ASSET.fullmatch(name)
        if match and match[1] != item["sha256"]:
            raise RecipeError("portable asset name does not match its digest")
        seen.add(name)
        total += item["bytes"]
    if "records.jsonl" not in seen or total > MAX_EXPANDED_BYTES:
        raise RecipeError("portable archive inventory is incomplete or oversized")
    return value


@contextmanager
def _regular_file(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise RecipeError("portable input must be a regular file")
            yield handle
    except OSError as exc:
        raise RecipeError("portable input is unavailable") from exc


def _directory_bound(handle: BinaryIO) -> None:
    """Reject huge/multidisk/ZIP64 directories before ZipFile allocates entries."""
    handle.seek(0, 2)
    size = handle.tell()
    if size > MAX_ARCHIVE_BYTES:
        raise RecipeError("portable archive is too large")
    handle.seek(max(0, size - 65557))
    tail = handle.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise RecipeError("portable archive directory is missing")
    if offset >= 20 and tail[offset - 20:offset - 16] == b"PK\x06\x07":
        raise RecipeError("portable ZIP64 archives are unsupported")
    _, disk, cd_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack("<4s4H2LH", tail[offset:offset + 22])
    if disk or cd_disk or disk_count != count or count > MAX_MEMBERS or cd_size > MAX_MANIFEST_BYTES or cd_offset + cd_size > size or offset + 22 + comment != len(tail):
        raise RecipeError("portable archive directory is invalid or oversized")
    actual_start = size - len(tail) + offset - cd_size
    if actual_start != cd_offset or actual_start < 0:
        raise RecipeError("portable archive directory offset differs")
    handle.seek(actual_start)
    directory = handle.read(cd_size)
    position = actual_count = 0
    while position < len(directory):
        if len(directory) - position < 46 or directory[position:position + 4] != b"PK\x01\x02":
            raise RecipeError("portable archive directory entry is invalid")
        if struct.unpack_from("<H", directory, position + 6)[0] >= 45:
            raise RecipeError("portable ZIP64/member version is unsupported")
        if struct.unpack_from("<H", directory, position + 34)[0]:
            raise RecipeError("portable multidisk members are unsupported")
        name_bytes, extra_bytes, comment_bytes = struct.unpack_from("<3H", directory, position + 28)
        position += 46 + name_bytes + extra_bytes + comment_bytes
        actual_count += 1
        if actual_count > MAX_MEMBERS or position > len(directory):
            raise RecipeError("portable archive directory entries are oversized")
    if actual_count != count:
        raise RecipeError("portable archive directory count differs")
    handle.seek(0)


def _reject_zip64_extra(extra: bytes) -> None:
    while extra:
        if len(extra) < 4:
            raise RecipeError("portable ZIP extra field is invalid")
        field, size = struct.unpack("<HH", extra[:4])
        if field == 1:
            raise RecipeError("portable ZIP64 members are unsupported")
        if len(extra) < size + 4:
            raise RecipeError("portable ZIP extra field is truncated")
        extra = extra[size + 4:]


def _check_local_header(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    archive.fp.seek(info.header_offset)
    header = archive.fp.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise RecipeError("portable ZIP local header is invalid")
    fields = struct.unpack("<4s5H3L2H", header)
    if fields[1] >= 45:
        raise RecipeError("portable ZIP64/member version is unsupported")
    archive.fp.read(fields[-2])
    _reject_zip64_extra(archive.fp.read(fields[-1]))


class PortableArchive:
    """A validated file inventory; all reads stay inside the opened ZIP handle."""

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.entries = {}
        total = 0
        for info in archive.infolist():
            name = info.filename
            maximum = _limit(name)
            if info.extract_version >= 45:
                raise RecipeError("portable ZIP64/member version is unsupported")
            _reject_zip64_extra(info.extra)
            mode = info.external_attr >> 16
            if name != info.orig_filename or name in self.entries or info.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG} or info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise RecipeError("portable archive contains an unsafe member")
            if info.file_size < 0 or info.file_size > maximum:
                raise RecipeError("portable archive member is oversized")
            self.entries[name] = info
            total += info.file_size
        if not 2 <= len(self.entries) <= MAX_MEMBERS or total > MAX_EXPANDED_BYTES or "manifest.json" not in self.entries:
            raise RecipeError("portable archive is incomplete or oversized")
        self.manifest = _manifest(_json(self._read("manifest.json")))
        self.inventory = {item["path"]: item for item in self.manifest["files"]}
        if set(self.entries) != {"manifest.json", *self.inventory}:
            raise RecipeError("portable archive inventory differs from its contents")
        for name, item in self.inventory.items():
            if item["bytes"] != self.entries[name].file_size:
                raise RecipeError("portable archive declared size differs")

    def chunks(self, name: str) -> Iterator[bytes]:
        if name not in self.entries:
            raise RecipeError("portable archive member is missing")
        digest = hashlib.sha256()
        size = 0
        try:
            _check_local_header(self.archive, self.entries[name])
            with self.archive.open(self.entries[name]) as handle:
                while chunk := handle.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > _limit(name):
                        raise RecipeError("portable archive expansion is oversized")
                    digest.update(chunk)
                    yield chunk
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
            raise RecipeError("portable archive member is corrupt") from exc
        if name != "manifest.json":
            expected = self.inventory[name]
            if size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
                raise RecipeError("portable archive member checksum differs")

    def _read(self, name: str) -> bytes:
        return b"".join(self.chunks(name))

    def read_asset(self, asset_id: str) -> bytes:
        if not isinstance(asset_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset_id):
            raise RecipeError("portable asset identifier is invalid")
        return self._read(f"assets/{asset_id[7:]}.jpg")

    def verify(self) -> dict:
        """Complete integrity pass before bank mutation; no archive extraction."""
        for name in self.inventory:
            for _ in self.chunks(name):
                pass
        count = sum(1 for _ in self.records())
        return {"records_count": count, "files_count": len(self.inventory),
                "expanded_bytes": sum(item["bytes"] for item in self.inventory.values())}

    def records(self) -> Iterator[dict]:
        pending = b""
        count = 0
        identities = set()
        for chunk in self.chunks("records.jsonl"):
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if not line or len(line) > MAX_RECORD_BYTES:
                    raise RecipeError("portable record line is empty or oversized")
                value = _json(line)
                if not isinstance(value, dict) or set(value) != {"recipe_id", "status", "recipe"}:
                    raise RecipeError("portable record envelope is invalid")
                identity = _text(value["recipe_id"], "recipe_id")
                if identity in identities:
                    raise RecipeError("portable recipe identity is duplicated")
                identities.add(identity)
                recipe = value["recipe"]
                if not isinstance(value["status"], str) or value["status"] not in {"ready", "draft"} or not isinstance(recipe, dict) or type(recipe.get("schema_version")) is not int or recipe["schema_version"] != self.manifest["recipe_schema_version"] or len(canonical_bytes(recipe)) > MAX_RECIPE_BYTES:
                    raise RecipeError("portable recipe document/status is invalid")
                image = recipe.get("image")
                if image is not None:
                    asset_id = image.get("asset_id") if isinstance(image, dict) else None
                    if not isinstance(asset_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset_id) or f"assets/{asset_id[7:]}.jpg" not in self.inventory:
                        raise RecipeError("portable recipe references a missing asset")
                count += 1
                if count > self.manifest["records_count"]:
                    raise RecipeError("portable archive has extra recipes")
                yield value
            if len(pending) > MAX_RECORD_BYTES:
                raise RecipeError("portable record line is oversized")
        if pending or count != self.manifest["records_count"]:
            raise RecipeError("portable records are truncated or count differs")


@contextmanager
def open_archive(path: Path | str):
    try:
        with _regular_file(Path(path)) as handle:
            _directory_bound(handle)
            with zipfile.ZipFile(handle) as archive:
                yield PortableArchive(archive)
    except (zipfile.BadZipFile, EOFError, UnicodeError, struct.error, NotImplementedError) as exc:
        raise RecipeError("portable archive is invalid") from exc


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def write_archive(destination: Path | str, manifest: Mapping, files: Mapping[str, Path]) -> dict:
    """Package caller-selected regular files; refuse replacement, then self-verify.

    Source paths are explicit local builder inputs, never paths from a manifest.
    Managed JPEG bytes must already have passed RecipeAssets. This writer makes
    no publication/rights decision and never copies directories implicitly.
    """
    value = dict(manifest)
    value["files"] = []
    for name, path in sorted(files.items()):
        maximum = _limit(name)
        if name == "manifest.json":
            raise RecipeError("manifest must not inventory itself")
        size = 0
        digest = hashlib.sha256()
        with _regular_file(Path(path)) as handle:
            while chunk := handle.read(CHUNK_BYTES):
                size += len(chunk)
                if size > maximum:
                    raise RecipeError("portable builder input is oversized")
                digest.update(chunk)
        value["files"].append({"path": name, "bytes": size, "sha256": digest.hexdigest()})
    _manifest(value)
    encoded = canonical_bytes(value)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise RecipeError("portable manifest is oversized")
    output = Path(destination)
    created = False
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "wb") as handle:
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=False) as archive:
                archive.writestr(_zip_info("manifest.json"), encoded, compresslevel=6)
                for item in value["files"]:
                    with _regular_file(Path(files[item["path"]])) as source, archive.open(_zip_info(item["path"]), "w") as target:
                        size = 0
                        while chunk := source.read(CHUNK_BYTES):
                            size += len(chunk)
                            if size > item["bytes"]:
                                raise RecipeError("portable builder input changed")
                            target.write(chunk)
                            if handle.tell() > MAX_ARCHIVE_BYTES:
                                raise RecipeError("portable output is oversized")
            handle.flush()
            os.fsync(handle.fileno())
        with open_archive(output) as archive:
            archive.verify()
        return value
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
