"""Defensive validation for uploaded images, run BEFORE anything else touches
the bytes (see app/routers/scans.py's create_scan, right after _sniff_mime).

Covers two distinct attack shapes:

1. Trailing/appended payload (polyglot files) — a byte-valid JPEG/PNG/WebP
   can still have anything appended after its real end-of-image marker.
   That's exactly how a polyglot file (image+HTML, image+ZIP, image+PHP) is
   built to slip past a filter that only checks the first few header bytes
   (which is all the pre-existing _sniff_mime check did). Everything
   genuinely part of the image — EXIF, ICC profile, embedded thumbnail —
   lives BEFORE that end marker, never after, so any bytes found after it
   are exactly the signal worth rejecting on.

2. Malformed structure / oversized metadata — files that don't parse as a
   coherent image at all, absurd pixel dimensions (decompression-bomb
   style), or a metadata segment far larger than any real camera/phone
   would ever produce (a plausible way to smuggle arbitrary data into a
   field that isn't the pixel data itself).

Fails fast and rejects outright — this never tries to strip/sanitize and
continue with a "cleaned" image, by design (see conversation: reject, don't
salvage).
"""

import io

from PIL import Image

# Real camera/phone EXIF is a few KB at most. Anything far beyond this is
# more plausibly data smuggled into a metadata field than a genuine tag.
_MAX_METADATA_SEGMENT_BYTES = 65_536
# ~40 megapixels - generous for any real product photo, but bounds the
# decode cost of a maliciously-crafted decompression-bomb style file.
_MAX_PIXELS = 40_000_000
# A few bytes of harmless padding after the true end-of-image marker is
# normal for some encoders. More than this is not padding.
_MAX_TRAILING_BYTES = 8


class UnsafeImageError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def validate_image_security(image_bytes: bytes, mime: str) -> None:
    _check_trailing_payload(image_bytes, mime)
    _check_structure_and_metadata(image_bytes)


def _check_trailing_payload(data: bytes, mime: str) -> None:
    if mime == "image/jpeg":
        end = data.rfind(b"\xff\xd9")  # last End-Of-Image marker
        if end == -1:
            raise UnsafeImageError("Image is malformed: no valid end-of-image marker found.")
        trailing = len(data) - (end + 2)
    elif mime == "image/png":
        end = data.find(b"IEND")
        if end == -1:
            raise UnsafeImageError("Image is malformed: no IEND chunk found.")
        trailing = len(data) - (end + 4 + 4)  # "IEND"(4) + its CRC(4)
    elif mime == "image/webp":
        if len(data) < 8:
            raise UnsafeImageError("Image is malformed.")
        declared_size = int.from_bytes(data[4:8], "little")  # RIFF chunk size field
        trailing = len(data) - (declared_size + 8)
    else:
        return

    if trailing > _MAX_TRAILING_BYTES:
        raise UnsafeImageError("Image rejected: data found appended after the end of the image (possible polyglot file).")


def _check_structure_and_metadata(data: bytes) -> None:
    try:
        Image.open(io.BytesIO(data)).verify()  # structural check only, no pixel decode
        img = Image.open(io.BytesIO(data))  # verify() closes the file object; reopen
        width, height = img.size
    except UnsafeImageError:
        raise
    except Exception as e:
        raise UnsafeImageError("Image failed structural validation and was rejected.") from e

    if width * height > _MAX_PIXELS:
        raise UnsafeImageError(f"Image dimensions ({width}x{height}) exceed the allowed maximum.")

    exif = img.info.get("exif")
    if exif and len(exif) > _MAX_METADATA_SEGMENT_BYTES:
        raise UnsafeImageError("Image metadata block is abnormally large.")
