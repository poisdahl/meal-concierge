"""Local, immutable recipe covers. References contain a digest, never a path.

Raw attachments are decoded and sanitized once. Pack/backup renditions use
install_managed instead: recompressing those would break frozen references.
No method fetches a URL or infers image rights from the photograph.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import re
import secrets
import stat
import warnings

from core import HouseholdError

MAX_INPUT_BYTES = 12 * 1024 * 1024
MAX_INPUT_PIXELS = 24_000_000
MAX_INPUT_EDGE = 12_000
MAX_RENDITION_EDGE = 1600
MAX_ASSET_BYTES = 4 * 1024 * 1024
ASSET_ID = re.compile(r"sha256:([0-9a-f]{64})\Z")
_JFIF = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


class RecipeAssetError(HouseholdError):
    pass


def _pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RecipeAssetError("recipe image support requires the installed Pillow runtime") from exc
    return Image, ImageOps


def _strict_jpeg(data: bytes, size: tuple[int, int]):
    # Pillow suppresses libjpeg's recoverable errors, accepting even an empty
    # entropy scan and manufacturing pixels. Reject those corrupt attachments
    # before fresh-pixel sanitization conceals the missing image data.
    try:
        import simplejpeg
    except ImportError as exc:
        raise RecipeAssetError("recipe image support requires the installed strict JPEG decoder") from exc
    try:
        height, width, _, _ = simplejpeg.decode_jpeg_header(data, strict=True)
        if (width, height) != size:
            raise RecipeAssetError("JPEG dimensions disagree between decoders")
        simplejpeg.decode_jpeg(data, strict=True)
    except ValueError as exc:
        raise RecipeAssetError("recipe JPEG has corrupt or incomplete image data") from exc


def asset_filename(asset_id: str) -> str:
    match = ASSET_ID.fullmatch(asset_id) if isinstance(asset_id, str) else None
    if not match:
        raise RecipeAssetError("invalid managed recipe asset reference")
    return match[1] + ".jpg"


@contextmanager
def _directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _read_at(directory: int, filename: str, maximum: int) -> bytes:
    # NONBLOCK prevents an attacker-supplied FIFO from hanging before fstat.
    descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise RecipeAssetError("recipe image must be a bounded regular file")
        value = handle.read(maximum + 1)
        if len(value) > maximum:
            raise RecipeAssetError("recipe image exceeds the byte limit")
        return value


def read_local_file(root: Path, relative_path: str, *, maximum=MAX_INPUT_BYTES) -> bytes:
    """Read an explicitly supplied attachment beneath its trusted import root.

    Each untrusted relative component is opened without following symlinks.
    The root is selected by the local operator, never downloaded recipe data.
    """
    if (not isinstance(relative_path, str) or not relative_path or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))):
        raise RecipeAssetError("image attachment must use a confined relative path")
    parts = relative_path.split("/")
    descriptors = []
    try:
        with _directory(root) as directory:
            for part in parts[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                descriptors.append(directory)
            return _read_at(directory, parts[-1], maximum)
    except (OSError, ValueError) as exc:
        raise RecipeAssetError("recipe image attachment is unavailable or outside its import root") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _dimensions(image, *, managed=False):
    width, height = image.size
    edge = MAX_RENDITION_EDGE if managed else MAX_INPUT_EDGE
    pixels = MAX_RENDITION_EDGE ** 2 if managed else MAX_INPUT_PIXELS
    if min(width, height) < 1 or max(width, height) > edge or width * height > pixels:
        raise RecipeAssetError("recipe image dimensions exceed the supported bounds")


def sanitize_image(data: bytes) -> bytes:
    if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        raise RecipeAssetError("recipe image exceeds the byte limit or is empty")
    Image, ImageOps = _pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as source:
                _dimensions(source)
                if getattr(source, "n_frames", 1) != 1:
                    raise RecipeAssetError("animated recipe covers are unsupported")
                if source.format == "JPEG":
                    _strict_jpeg(data, source.size)
                source.verify()
            with Image.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as source:
                source.load()  # verify() alone does not decode the raster.
                oriented = ImageOps.exif_transpose(source)
                try:
                    oriented.thumbnail((MAX_RENDITION_EDGE, MAX_RENDITION_EDGE), Image.Resampling.LANCZOS)
                    with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as clean:
                        clean.paste(rgba, mask=rgba.getchannel("A"))
                        output = io.BytesIO()
                        # Fresh pixels have no EXIF, ICC, comments, XMP or PNG text.
                        clean.save(output, "JPEG", quality=85, subsampling=2, optimize=False, progressive=False)
                        result = output.getvalue()
                finally:
                    oriented.close()
        validate_managed(result)
        return result
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise RecipeAssetError("recipe cover is corrupt or is not a supported static JPEG, PNG or WebP") from exc


def _clean_jpeg_markers(data: bytes):
    """Reject embedded JPEG metadata markers and payload after the image.

    Pillow's info/applist stops at the first scan, so it cannot alone establish
    that metadata or an unrelated payload was not appended after that scan.
    """
    if not data.startswith(b"\xff\xd8"):
        raise RecipeAssetError("managed cover must be a sanitized JPEG rendition")
    position = 2
    seen = set()
    while position + 4 <= len(data):
        if data[position] != 255:
            break
        marker = data[position + 1]
        length = int.from_bytes(data[position + 2:position + 4], "big")
        end = position + 2 + length
        if length < 2 or end > len(data) or marker not in {0xE0, 0xDB, 0xC4, 0xC0, 0xDA}:
            break
        if marker == 0xE0 and (marker in seen or data[position + 4:end] != _JFIF):
            break
        if marker == 0xC0 and marker in seen:
            break
        seen.add(marker)
        position = end
        if marker != 0xDA:
            continue
        if not {0xDB, 0xC4, 0xC0}.issubset(seen):
            break
        # Our encoder emits no restart markers or additional scans.
        while position < len(data):
            position = data.find(b"\xff", position)
            if position < 0 or position + 1 >= len(data):
                break
            following = data[position + 1]
            if following == 0:
                position += 2
                continue
            if following == 0xD9 and position + 2 == len(data):
                return
            break
        break
    raise RecipeAssetError("managed JPEG contains unsupported markers, metadata or trailing data")


def validate_managed(data: bytes, asset_id: str | None = None):
    if not isinstance(data, bytes) or not data or len(data) > MAX_ASSET_BYTES:
        raise RecipeAssetError("managed recipe cover exceeds the byte limit or is empty")
    if asset_id is not None:
        asset_filename(asset_id)
        if "sha256:" + hashlib.sha256(data).hexdigest() != asset_id:
            raise RecipeAssetError("managed recipe cover digest does not match its reference")
    _clean_jpeg_markers(data)
    Image, _ = _pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=("JPEG",)) as image:
                _dimensions(image, managed=True)
                if image.mode != "RGB":
                    raise RecipeAssetError("managed recipe cover must contain RGB pixels")
                _strict_jpeg(data, image.size)
                image.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise RecipeAssetError("managed recipe cover is corrupt") from exc


class RecipeAssets:
    def __init__(self, root: Path):
        self.root = Path(root)

    def import_file(self, import_root: Path, relative_path: str) -> str:
        return self.import_bytes(read_local_file(import_root, relative_path))

    def import_bytes(self, data: bytes) -> str:
        rendition = sanitize_image(data)
        asset_id = "sha256:" + hashlib.sha256(rendition).hexdigest()
        self.install_managed(asset_id, rendition)
        return asset_id

    def install_managed(self, asset_id: str, data: bytes) -> None:
        """Restore exact bytes from a trusted private backup or verified pack.

        The caller establishes artifact provenance. A matching digest is an
        integrity check, not that authority. Ordinary external attachments must
        use import_file/import_bytes, which create a fresh sanitized rendition.
        """
        filename = asset_filename(asset_id)
        validate_managed(data, asset_id)
        temporary = ".import-" + secrets.token_hex(16)
        try:
            self.root.mkdir(mode=0o700, exist_ok=True)
            with _directory(self.root) as directory:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    try:
                        os.link(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError:
                        # Never overwrite an old asset, even if its bytes are corrupt.
                        validate_managed(_read_at(directory, filename, MAX_ASSET_BYTES), asset_id)
                    os.fsync(directory)
                finally:
                    os.unlink(temporary, dir_fd=directory)
        except OSError as exc:
            raise RecipeAssetError("managed recipe asset storage is unavailable") from exc

    def read(self, asset_id: str) -> bytes:
        filename = asset_filename(asset_id)
        try:
            with _directory(self.root) as directory:
                data = _read_at(directory, filename, MAX_ASSET_BYTES)
            validate_managed(data, asset_id)
            return data
        except OSError as exc:
            raise RecipeAssetError("managed recipe cover is missing or unavailable") from exc
