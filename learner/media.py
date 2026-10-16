"""Images on cards (DESIGN.md §12.1): validation, storage, and finding the images a card uses.

Images live in the database, keyed by the SHA-256 of their bytes, so the same image is stored once.
Cards reference them in Markdown: ![description](/media/<sha256>).
"""

from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

MEDIA_URL = re.compile(r"/media/([0-9a-f]{64})")

# Identified from the file's first bytes, never from its name or the browser's claim.
SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class MediaError(ValueError):
    """An image we won't store; the message is shown to you."""


@dataclass(frozen=True)
class Image:
    sha256: str
    mime: str
    data: bytes
    filename: str
    alt: str

    @property
    def url(self) -> str:
        return f"/media/{self.sha256}"


def detect_mime(data: bytes) -> str | None:
    for signature, mime in SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def save_image(conn: sqlite3.Connection, data: bytes, filename: str = "", alt: str = "", max_bytes: int = 4_000_000) -> Image:
    """Store an image (or find the identical one already stored). Raises MediaError for anything else."""
    if not data:
        raise MediaError("That file is empty.")
    if len(data) > max_bytes:
        raise MediaError(f"That image is {len(data) / 1e6:.1f} MB; the limit is {max_bytes / 1e6:.0f} MB.")
    mime = detect_mime(data)
    if mime is None:
        raise MediaError("Only JPEG, PNG, GIF and WebP images can be added.")
    sha = hashlib.sha256(data).hexdigest()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO media (sha256, mime, bytes, filename, alt, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sha, mime, data, filename[:200], alt[:500], datetime.now(timezone.utc).isoformat(timespec="microseconds")),
        )
    return get_image(conn, sha)


def get_image(conn: sqlite3.Connection, sha256: str) -> Image | None:
    row = conn.execute("SELECT sha256, mime, bytes, filename, alt FROM media WHERE sha256 = ?", (sha256,)).fetchone()
    return Image(row[0], row[1], bytes(row[2]), row[3], row[4]) if row else None


def referenced(*texts: str) -> list[str]:
    """Image fingerprints referenced in the given Markdown, in order of appearance, without repeats."""
    seen: dict[str, None] = {}
    for text in texts:
        for sha in MEDIA_URL.findall(text or ""):
            seen[sha] = None
    return list(seen)


def images_for(conn: sqlite3.Connection, *texts: str) -> list[Image]:
    return [img for sha in referenced(*texts) if (img := get_image(conn, sha))]


def to_export(image: Image) -> dict:
    return {"mime": image.mime, "data": base64.b64encode(image.data).decode("ascii"),
            "filename": image.filename, "alt": image.alt}


def decode_export(sha256: str, item: dict) -> bytes:
    """An exported image's bytes, checked against its fingerprint. Raises MediaError."""
    try:
        data = base64.b64decode(item["data"], validate=True)
    except (KeyError, ValueError) as exc:
        raise MediaError(f"Image {sha256[:12]}…: the image data is unreadable.") from exc
    if hashlib.sha256(data).hexdigest() != sha256:
        raise MediaError(f"Image {sha256[:12]}…: the data doesn't match its fingerprint.")
    if detect_mime(data) is None:
        raise MediaError(f"Image {sha256[:12]}…: not a JPEG, PNG, GIF or WebP image.")
    return data
