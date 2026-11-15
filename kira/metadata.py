"""Capture metadata for photos, RAW files and videos, read without decoding pixels.

Kira identifies a photo by when and on what it was taken. Pillow cannot open
most camera RAW files, so this module parses the TIFF/EXIF structure directly
and works the same way for JPEG, TIFF-based RAW (ARW, NEF, CR2, DNG, ...),
HEIC and QuickTime video. It also extracts the JPEG preview embedded in a RAW.
"""

from __future__ import annotations

import os
import re
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from .media import (
    JPEG_EXTENSIONS,
    PHOTO_EXTENSIONS,
    PROJECT_EXTENSIONS,
    RAW_EXTENSIONS,
    SIDECAR_EXTENSIONS,
    VIDEO_EXTENSIONS,
)

HEIF_EXTENSIONS = {".heic", ".heif"}
_SCAN_LIMIT = 64 * 1024 * 1024
_MAX_IFDS = 32
_MAX_ENTRIES = 2000
_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_TIFF_MAGIC = {42, 85, 0x4F52, 0x5352}  # TIFF, Panasonic RW2, Olympus ORF variants

EDITOR_SOFTWARE = re.compile(
    r"lightroom|photoshop|camera raw|snapseed|vsco|darkroom|pixelmator|photomator|affinity|capture one|darktable"
    r"|gimp|luminar|picsart|afterlight|rawtherapee|acdsee|dxo|on1 |polarr|skylum|topaz|picasa|paint\.net|krita"
    r"|photo editor|facetune|lensa|remini",
    re.IGNORECASE,
)
SHARED_SOFTWARE = re.compile(r"instagram|whatsapp|facebook|messenger|telegram|snapchat|tiktok|wechat", re.IGNORECASE)
_EDIT_XMP_MARKERS = (b"xmpMM:History", b"xmpMM:DerivedFrom", b"stEvt:", b"crs:Version", b"crs:ProcessVersion")
_PHONE_MAKES = re.compile(
    r"apple|google|samsung|xiaomi|huawei|oneplus|oppo|vivo|motorola|nokia|hmd|realme|honor|nothing|fairphone|zte",
    re.IGNORECASE,
)
_QUICKTIME_LOCAL = re.compile(rb"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?([+-]\d\d:?\d\d|Z)")


def file_kind(path: Path | str) -> str:
    """Classify a file by extension: photo, video, sidecar, project or other."""
    suffix = os.path.splitext(str(path))[1].casefold()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    if suffix in SIDECAR_EXTENSIONS:
        return "sidecar"
    if suffix in PROJECT_EXTENSIONS:
        return "project"
    return "other"


def empty_metadata(kind: str = "other") -> dict:
    return {
        "kind": kind,
        "make": "",
        "model": "",
        "software": "",
        "capture_time": "",
        "subsec": "",
        "offset": "",
        "width": 0,
        "height": 0,
        "orientation": 1,
        "edited": False,
        "shared": False,
    }


def origin_of(metadata: dict) -> str:
    """Phone, camera or other, judged from the camera make and model."""
    make = metadata.get("make", "")
    model = metadata.get("model", "")
    if not make and not model:
        return "other"
    if _PHONE_MAKES.search(make) or re.match(r"iphone|ipad|pixel|sm-|galaxy", model, re.IGNORECASE):
        return "phone"
    return "camera"


class _Tiff:
    """Minimal bounds-checked reader for a TIFF structure at ``base`` inside a file."""

    def __init__(self, handle: BinaryIO, base: int, size: int) -> None:
        self.handle = handle
        self.base = base
        self.size = size
        header = self._read(0, 8)
        if header[:2] == b"II":
            self.order = "<"
        elif header[:2] == b"MM":
            self.order = ">"
        else:
            raise ValueError("not a TIFF structure")
        if struct.unpack(self.order + "H", header[2:4])[0] not in _TIFF_MAGIC:
            raise ValueError("not a TIFF structure")
        self.first_ifd = struct.unpack(self.order + "I", header[4:8])[0]

    def _read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or self.base + offset + length > self.size:
            raise ValueError("TIFF offset is outside the file")
        self.handle.seek(self.base + offset)
        data = self.handle.read(length)
        if len(data) != length:
            raise ValueError("TIFF structure is truncated")
        return data

    def ifd(self, offset: int) -> tuple[dict[int, object], int]:
        """Return the decoded entries of one directory and the offset of the next."""
        count = struct.unpack(self.order + "H", self._read(offset, 2))[0]
        if count > _MAX_ENTRIES:
            raise ValueError("TIFF directory is implausibly large")
        raw = self._read(offset + 2, count * 12 + 4)
        entries: dict[int, object] = {}
        for index in range(count):
            tag, kind, length = struct.unpack(self.order + "HHI", raw[index * 12 : index * 12 + 8])
            field = raw[index * 12 + 8 : index * 12 + 12]
            unit = _TYPE_SIZES.get(kind)
            if unit is None or length > 1_000_000:
                continue
            total = unit * length
            try:
                data = field[:total] if total <= 4 else self._read(struct.unpack(self.order + "I", field)[0], total)
            except ValueError:
                continue
            entries[tag] = self._decode(kind, length, data)
        next_offset = struct.unpack(self.order + "I", raw[count * 12 : count * 12 + 4])[0]
        return entries, next_offset

    def _decode(self, kind: int, length: int, data: bytes) -> object:
        if kind == 2:
            return data.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()
        if kind in {1, 6, 7}:
            return data
        code = {3: "H", 4: "I", 8: "h", 9: "i", 13: "I"}.get(kind)
        if code is None:
            return data
        values = struct.unpack(self.order + code * length, data)
        return values[0] if length == 1 else list(values)


def _as_list(value: object) -> list[int]:
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, int)]
    return []


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _number(value: object) -> int:
    return value if isinstance(value, int) else 0


def _read_tiff(handle: BinaryIO, base: int, size: int, metadata: dict, previews: list[tuple[int, int]]) -> None:
    """Fill ``metadata`` from a TIFF structure and collect embedded JPEG ranges."""
    tiff = _Tiff(handle, base, size)
    pending = [(tiff.first_ifd, "main")]
    visited: set[int] = set()
    largest_area = 0
    while pending and len(visited) < _MAX_IFDS:
        offset, role = pending.pop(0)
        if not offset or offset in visited:
            continue
        visited.add(offset)
        try:
            entries, next_offset = tiff.ifd(offset)
        except (ValueError, struct.error):
            continue
        if role == "main" and next_offset:
            pending.append((next_offset, "main"))
        if role == "exif":
            metadata["capture_time"] = (
                _text(entries.get(0x9003)) or _text(entries.get(0x9004)) or metadata["capture_time"]
            )
            metadata["subsec"] = _text(entries.get(0x9291)) or metadata["subsec"]
            metadata["offset"] = _text(entries.get(0x9011)) or metadata["offset"]
            width, height = _number(entries.get(0xA002)), _number(entries.get(0xA003))
            if width and height and not metadata["width"]:
                metadata["width"], metadata["height"] = width, height
            continue
        if offset == tiff.first_ifd:
            metadata["make"] = _text(entries.get(0x010F))
            metadata["model"] = _text(entries.get(0x0110))
            metadata["software"] = _text(entries.get(0x0131))
            metadata["orientation"] = _number(entries.get(0x0112)) or 1
        width, height = _number(entries.get(0x0100)), _number(entries.get(0x0101))
        if width * height > largest_area:
            largest_area = width * height
            metadata["width"], metadata["height"] = width, height
        if 0x8769 in entries:
            pending.append((_number(entries[0x8769]), "exif"))
        for sub in _as_list(entries.get(0x014A)):
            pending.append((sub, "sub"))
        start, length = _number(entries.get(0x0201)), _number(entries.get(0x0202))
        if start and length:
            previews.append((base + start, length))
        strips, counts = _as_list(entries.get(0x0111)), _as_list(entries.get(0x0117))
        if _number(entries.get(0x0103)) in {6, 7} and len(strips) == 1 and len(counts) == 1:
            previews.append((base + strips[0], counts[0]))


def _read_jpeg(handle: BinaryIO, size: int, metadata: dict) -> None:
    """Walk JPEG segments for EXIF, XMP edit markers and the frame size."""
    handle.seek(0)
    if handle.read(2) != b"\xff\xd8":
        return
    position = 2
    while position + 4 <= size:
        handle.seek(position)
        marker = handle.read(4)
        if len(marker) < 4 or marker[0] != 0xFF:
            return
        code = marker[1]
        if code in {0xD8, 0x01} or 0xD0 <= code <= 0xD7:
            position += 2
            continue
        length = struct.unpack(">H", marker[2:4])[0]
        if length < 2:
            return
        start = position + 4
        if code == 0xE1:
            head = handle.read(min(length - 2, 32))
            if head.startswith(b"Exif\x00\x00"):
                try:
                    _read_tiff(handle, start + 6, min(size, start + length - 2), metadata, [])
                except (ValueError, struct.error):
                    pass
            elif head.startswith(b"http://ns.adobe.com/xap/1.0/"):
                handle.seek(start)
                packet = handle.read(length - 2)
                if any(mark in packet for mark in _EDIT_XMP_MARKERS):
                    metadata["edited"] = True
        elif 0xC0 <= code <= 0xCF and code not in {0xC4, 0xC8, 0xCC}:
            frame = handle.read(5)
            if len(frame) == 5:
                metadata["height"], metadata["width"] = struct.unpack(">HH", frame[1:5])
        if code == 0xDA:
            return
        position = start + length - 2


def _scan_for_exif(handle: BinaryIO, size: int, metadata: dict) -> None:
    """Find an EXIF block inside a container (HEIC, RAF, ...) by its signature."""
    handle.seek(0)
    data = handle.read(min(size, _SCAN_LIMIT))
    for match in re.finditer(rb"Exif\x00\x00(?:II\x2a\x00|MM\x00\x2a)", data):
        try:
            _read_tiff(handle, match.start() + 6, size, metadata, [])
        except (ValueError, struct.error):
            continue
        if metadata["capture_time"] or metadata["model"]:
            return


def _atoms(handle: BinaryIO, start: int, end: int):
    position = start
    while position + 8 <= end:
        handle.seek(position)
        header = handle.read(8)
        if len(header) < 8:
            return
        length, name = struct.unpack(">I4s", header)
        body = position + 8
        if length == 1:
            extended = handle.read(8)
            if len(extended) < 8:
                return
            length = struct.unpack(">Q", extended)[0]
            body = position + 16
        elif length == 0:
            length = end - position
        if length < 8 or position + length > end:
            return
        yield name, body, position + length
        position += length


def _read_quicktime(handle: BinaryIO, size: int, metadata: dict) -> None:
    for name, body, end in _atoms(handle, 0, size):
        if name != b"moov":
            continue
        if end - body <= 8 * 1024 * 1024:
            handle.seek(body)
            local = _QUICKTIME_LOCAL.search(handle.read(end - body))
            if local:
                parts = local.groups()
                metadata["capture_time"] = "{}:{}:{} {}:{}:{}".format(*(item.decode() for item in parts[:6]))
                metadata["offset"] = parts[6].decode()
        for child, child_body, _child_end in _atoms(handle, body, end):
            if child != b"mvhd":
                continue
            handle.seek(child_body)
            head = handle.read(12)
            if len(head) < 12:
                break
            seconds = struct.unpack(">Q", head[4:12])[0] if head[0] == 1 else struct.unpack(">I", head[4:8])[0]
            if seconds and not metadata["capture_time"]:
                try:
                    taken = datetime(1904, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)
                    metadata["capture_time"] = taken.astimezone().strftime("%Y:%m:%d %H:%M:%S")
                except (OverflowError, OSError, ValueError):
                    pass
        return


def _finish(metadata: dict) -> dict:
    time = metadata["capture_time"]
    # Cameras with an unset clock and tools that write a zero timestamp give years before 1980.
    if not re.fullmatch(r"\d{4}:\d\d:\d\d \d\d:\d\d:\d\d", time) or time < "1980":
        metadata["capture_time"] = ""
    software = metadata["software"]
    if EDITOR_SOFTWARE.search(software):
        metadata["edited"] = True
    if SHARED_SOFTWARE.search(software):
        metadata["shared"] = True
    if metadata["orientation"] in {5, 6, 7, 8}:
        metadata["width"], metadata["height"] = metadata["height"], metadata["width"]
    return metadata


def read_metadata(path: Path) -> dict:
    """Read capture metadata; unreadable or unknown files yield empty values."""
    kind = file_kind(path)
    metadata = empty_metadata(kind)
    if kind not in {"photo", "video"}:
        return metadata
    suffix = path.suffix.casefold()
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(16)
            if kind == "video":
                _read_quicktime(handle, size, metadata)
            elif head[:2] == b"\xff\xd8":
                _read_jpeg(handle, size, metadata)
            elif head[:2] in {b"II", b"MM"}:
                _read_tiff(handle, 0, size, metadata, [])
            elif suffix in HEIF_EXTENSIONS or suffix in RAW_EXTENSIONS or head[4:8] == b"ftyp":
                _scan_for_exif(handle, size, metadata)
            else:
                _read_with_pillow(path, metadata)
    except (OSError, ValueError, struct.error):
        pass
    return _finish(metadata)


def _read_with_pillow(path: Path, metadata: dict) -> None:
    """PNG, WebP and AVIF keep EXIF in container chunks that Pillow already parses."""
    from PIL import Image

    try:
        with Image.open(path) as image:
            metadata["width"], metadata["height"] = image.size
            exif = image.getexif()
            metadata["make"] = str(exif.get(0x010F) or "").strip("\x00 ")
            metadata["model"] = str(exif.get(0x0110) or "").strip("\x00 ")
            metadata["software"] = str(exif.get(0x0131) or "").strip("\x00 ")
            detail = exif.get_ifd(0x8769)
            metadata["capture_time"] = str(detail.get(0x9003) or detail.get(0x9004) or "").strip("\x00 ")
            metadata["subsec"] = str(detail.get(0x9291) or "").strip("\x00 ")
    except Exception:  # noqa: BLE001 - any undecodable image simply has no metadata
        return


def embedded_preview(path: Path) -> bytes | None:
    """Return the largest JPEG stored inside a TIFF-based RAW file, if any."""
    if path.suffix.casefold() in JPEG_EXTENSIONS:
        return None
    previews: list[tuple[int, int]] = []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if handle.read(2) not in {b"II", b"MM"}:
                return None
            _read_tiff(handle, 0, size, empty_metadata("photo"), previews)
            for start, length in sorted(previews, key=lambda item: item[1], reverse=True):
                if start + length > size or length < 4 or length > 64 * 1024 * 1024:
                    continue
                handle.seek(start)
                data = handle.read(length)
                if data[:2] == b"\xff\xd8":
                    return data
    except (OSError, ValueError, struct.error):
        return None
    return None
