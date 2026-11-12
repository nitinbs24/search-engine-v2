"""Prepares an image for the model server and builds its searchable text.

The model's own preprocessor does the resize and crop, so this only bounds huge images,
applies the EXIF rotation and drops alpha/palette modes.
"""
import io

from PIL import Image, ImageOps

MAX_SIDE = 2048  # bounds the upload and the decode cost; well above any CLIP input size
EXIF_IFD, GPS_IFD = 0x8769, 0x8825


def _degrees(dms, ref) -> float:
    d, m, s = (float(x) for x in dms)
    return (d + m / 60 + s / 3600) * (-1 if ref in ("S", "W") else 1)


def prepare(data: bytes) -> tuple[bytes, dict]:
    """Returns (JPEG bytes for the model server, EXIF facts). Raises if the bytes are not an image."""
    image = Image.open(io.BytesIO(data))
    image.load()
    exif = image.getexif()  # read before transposing, which drops it
    taken = exif.get_ifd(EXIF_IFD).get(36867) or exif.get(306)
    facts = {"camera": " ".join(str(exif[t]) for t in (271, 272) if exif.get(t)), "taken": taken}
    gps = exif.get_ifd(GPS_IFD)
    if gps.get(2) and gps.get(4):
        try:
            facts["location"] = f"{_degrees(gps[2], gps.get(1)):.4f}, {_degrees(gps[4], gps.get(3)):.4f}"
        except (TypeError, ValueError):
            pass  # malformed GPS tags are not worth failing the upload
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=90)
    return out.getvalue(), {k: v for k, v in facts.items() if v}


def describe(filename: str, facts: dict) -> str:
    parts = [f"Image: {filename}"]
    if "camera" in facts:
        parts.append(f"Camera: {facts['camera']}.")
    if "taken" in facts:
        parts.append(f"Date: {facts['taken']}.")
    if "location" in facts:
        parts.append(f"Location: {facts['location']}.")
    return " ".join(parts)
