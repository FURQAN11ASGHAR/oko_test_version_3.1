import io

from PIL import Image
from pyzbar.pyzbar import ZBarSymbol, decode

_EAN_SYMBOLS = [ZBarSymbol.EAN13, ZBarSymbol.EAN8, ZBarSymbol.UPCA]


def decode_gtin(image_bytes: bytes) -> str | None:
    """Real decoder ahead of the vision model (spec §1.2): fast, free,
    deterministic, and fails honestly instead of hallucinating a check-digit-
    valid GTIN."""
    img = Image.open(io.BytesIO(image_bytes))
    results = decode(img, symbols=_EAN_SYMBOLS)
    if not results:
        return None
    return results[0].data.decode("ascii")
