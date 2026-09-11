"""Downscales oversized images before they're sent to the vision model —
purely an efficiency measure (smaller upload, fewer vision tokens, faster
round trip), run AFTER app/pipeline/image_security.py has already validated
the original upload is structurally sound and payload-free.

Only affects the copy of the bytes sent to vision_identify(); the original
bytes (and their sha256) are untouched everywhere else — GTIN/barcode
decoding in particular still runs on the full-resolution original, since
downscaling a small barcode region is a real risk to read accuracy that
buys nothing (barcodes don't need 1080p to decode).
"""

import cv2
import numpy as np

# Threshold on the longer side, in pixels, not file size — this is a
# resolution cap, matched to what a "1080" figure normally means for a
# photo.
_MAX_DIMENSION = 1080
_JPEG_QUALITY = 92


def maybe_downscale_for_vision(image_bytes: bytes, mime: str) -> tuple[bytes, str, bool]:
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        # Already validated upstream (image_security.py); if OpenCV still
        # can't decode it for some reason, don't block the scan on a
        # purely-optional efficiency step — fall back to the original.
        return image_bytes, mime, False

    height, width = img.shape[:2]
    longest_side = max(height, width)
    if longest_side <= _MAX_DIMENSION:
        return image_bytes, mime, False

    scale = _MAX_DIMENSION / longest_side
    new_size = (round(width * scale), round(height * scale))

    # cv2.INTER_AREA is OpenCV's area-based resampling for shrinking: each
    # output pixel is the weighted average of the source pixels it covers
    # — this IS average pooling for downscaling (as opposed to nearest/
    # bilinear, which just samples/interpolates and throws detail away
    # rather than averaging it in). Recommended by OpenCV specifically for
    # this direction of resize.
    resized = cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)

    ok, encoded = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
    if not ok:
        return image_bytes, mime, False
    return encoded.tobytes(), "image/jpeg", True
