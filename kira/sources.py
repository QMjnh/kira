"""Browse a card or any folder outside the library, without changing it.

Kira hashes the files once (several at a time) and remembers the hashes, so each file can
show whether the library already holds it and whether the same bytes sit elsewhere on the
same drive. Two clean-ups follow from that, and neither erases anything:

* On another drive (a card), duplicates are never moved. Kira writes a ``_trash`` folder of
  shortcuts that point at them, with a note saying which copy each one repeats.
* On the library drive, a file outside the library whose exact bytes are already in the
  library can be moved into the library trash, with the reason written next to it.
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

from .errors import KiraError
from .library import SKIPPED_SOURCE_DIRS, TRASH_DIR, Library
from .media import RAW_EXTENSIONS, natural_key, resolve_directory, sha256_file, utc_now
from .metadata import file_kind
from .tasks import ordered_parallel

SCAN_WORKERS = 6
LINKS_DIR = "_trash"
LINKS_NOTE = "DUPLICATES.txt"
OUTSIDE_DIR = "Outside the library"
WHY_NOTE = "WHY.txt"


def _visible(name: str) -> bool:
    return not name.startswith((".", "~"))


def _cover_rank(path: Path) -> int:
    """Which file best stands for a folder: a JPEG or HEIC, then RAW, then video."""
    kind = file_kind(path)
    if kind == "photo":
        return 1 if path.suffix.casefold() in RAW_EXTENSIONS else 0
    return 2 if kind == "video" else 3


def _folder_card(folder: Path, depth: int = 1) -> dict:
    """What a folder card shows: the media directly inside, its subfolders, and one file to show.

    A folder holding only subfolders (``DCIM``) borrows its cover from the first one that has media.
    """
    media, subfolders, best = 0, [], None
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not _visible(entry.name):
                    continue
                if entry.is_dir():
                    if entry.name.casefold() not in SKIPPED_SOURCE_DIRS:
                        subfolders.append(Path(entry.path))
                    continue
                path = Path(entry.path)
                rank = _cover_rank(path)
                if rank == 3:
                    continue
                media += 1
                if best is None or (rank, natural_key(path.name)) < best[:2]:
                    best = (rank, natural_key(path.name), str(path))
    except OSError:
        return {"media": 0, "folders": 0, "cover": None}
    cover = best[2] if best else None
    if cover is None and depth:
        for subfolder in sorted(subfolders, key=lambda item: natural_key(item.name)):
            cover = _folder_card(subfolder, depth - 1)["cover"]
            if cover:
                break
    return {"media": media, "folders": len(subfolders), "cover": cover}


def _keeper_order(item: dict) -> tuple:
    """The copy to keep: the shallowest folder, then the oldest file, then name order."""
    path = Path(item["path"])
    return (len(path.parts), item["mtime_ns"], natural_key(str(path)))


class SourceBrowser:
    def __init__(self, library: Library) -> None:
        self.library = library
        self.state = library.state
        self.lock = threading.RLock()
        self._groups: dict[str, list[dict]] = {}

    # -- Hashes ---------------------------------------------------------------------

    def _inside_library(self, path: Path) -> bool:
        root = self.library.root
        return root is not None and (path == root or path.is_relative_to(root))

    def _walk(self, root: Path):
        """Files under ``root``, never descending into the library itself."""
        for folder, directories, names in os.walk(root):
            directories[:] = [
                name
                for name in directories
                if name.casefold() not in SKIPPED_SOURCE_DIRS
                and _visible(name)
                and not self._inside_library(Path(folder) / name)
            ]
            for name in names:
                if _visible(name):
                    yield Path(folder) / name

    def _entries(self, root: Path, recursive: bool):
        """(path, stat) for each file in ``root``, and below it when ``recursive``.

        Read from directory listings alone: on Windows a listing carries each file's size and
        time, which spares a slow stat per file on a card holding thousands of them.
        """
        stack = [str(root)]
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as listing:
                    entries = list(listing)
            except OSError:
                continue
            for entry in entries:
                if not _visible(entry.name):
                    continue
                try:
                    if entry.is_dir():
                        if (
                            recursive
                            and entry.name.casefold() not in SKIPPED_SOURCE_DIRS
                            and not self._inside_library(Path(entry.path))
                        ):
                            stack.append(entry.path)
                        continue
                    yield entry.path, entry.stat()
                except OSError:
                    continue

    def _rows(self, where: str, parameters: tuple) -> dict[str, dict]:
        rows = self.state.execute(f"SELECT path, size, mtime_ns, sha256 FROM source_hashes WHERE {where}", parameters)
        return {row["path"]: dict(row) for row in rows}

    def _rows_under(self, folder: Path) -> dict[str, dict]:
        """Every remembered hash for a file inside ``folder`` (LIKE alone would treat ``_`` as a wildcard)."""
        prefix = str(folder) if str(folder).endswith(os.sep) else str(folder) + os.sep
        rows = self._rows("path LIKE ?", (f"{prefix}%",))
        return {path: row for path, row in rows.items() if path.casefold().startswith(prefix.casefold())}

    def _receipt(self, path: Path, size: int, mtime_ns: int) -> str | None:
        rows = self.state.execute("SELECT sha256 FROM receipts WHERE source_key=?", (f"{path}|{size}|{mtime_ns}",))
        return rows[0]["sha256"] if rows else None

    def scan(self, root: Path, progress: Callable | None = None, cancelled: threading.Event | None = None) -> dict:
        """Hash every file under ``root`` that changed since it was last hashed, several at a time."""
        root = resolve_directory(str(root))
        if self._inside_library(root):
            raise KiraError(HTTPStatus.BAD_REQUEST, "This folder is inside the library; Kira already knows its files")
        known = self._rows_under(root)
        todo: list[tuple[Path, int, int]] = []
        reused = 0
        files = list(self._walk(root))
        for path in files:
            info = path.stat()
            row = known.get(str(path))
            if row and row["size"] == info.st_size and row["mtime_ns"] == info.st_mtime_ns:
                continue
            receipt = self._receipt(path, info.st_size, info.st_mtime_ns)
            if receipt:
                self._remember(path, info.st_size, info.st_mtime_ns, receipt)
                reused += 1
                continue
            todo.append((path, info.st_size, info.st_mtime_ns))
        failed: list[dict] = []
        done = 0
        for (path, size, mtime_ns), digest, error in ordered_parallel(
            todo, lambda item: sha256_file(item[0]), SCAN_WORKERS, cancelled
        ):
            done += 1
            if error is not None:
                failed.append({"name": path.name, "error": str(error)})
            else:
                self._remember(path, size, mtime_ns, digest)
            if progress:
                progress(done, len(todo), path.name)
        with self.lock:
            self._groups.pop(root.anchor.casefold(), None)
        return {"total": len(files), "hashed": done - len(failed), "reused": reused, "failed": failed}

    def _remember(self, path: Path, size: int, mtime_ns: int, digest: str) -> None:
        self.state.execute(
            "INSERT OR REPLACE INTO source_hashes VALUES (?,?,?,?,?,?)",
            (str(path), str(path.parent), size, mtime_ns, digest, utc_now()),
        )

    # -- Duplicates -----------------------------------------------------------------

    def _current(self, rows: dict[str, dict]) -> list[dict]:
        """Rows whose file is still there unchanged, read one folder listing at a time (see ``_entries``)."""
        by_folder: dict[str, list[dict]] = {}
        for row in rows.values():
            by_folder.setdefault(os.path.dirname(row["path"]), []).append(row)
        current = []
        for folder, items in by_folder.items():
            try:
                with os.scandir(folder) as listing:
                    found = {entry.path.casefold(): entry for entry in listing}
            except OSError:
                continue
            for row in items:
                entry = found.get(row["path"].casefold())
                try:
                    info = entry.stat() if entry and not entry.is_dir() else None
                except OSError:
                    info = None
                if info and info.st_size == row["size"] and info.st_mtime_ns == row["mtime_ns"]:
                    current.append(row)
        return current

    def duplicate_groups(self, anchor: str) -> list[dict]:
        """Files with identical bytes on one drive, grouped, with the copy to keep first."""
        key = anchor.casefold()
        with self.lock:
            if key in self._groups:
                return self._groups[key]
        by_sha: dict[str, list[dict]] = {}
        for row in self._current(self._rows_under(Path(anchor))):
            path = Path(row["path"])
            if row["size"] > 0 and LINKS_DIR not in path.parts and file_kind(path) != "other":
                by_sha.setdefault(row["sha256"], []).append(row)
        groups = []
        for digest, items in by_sha.items():
            if len(items) > 1:
                items.sort(key=_keeper_order)
                groups.append({"sha256": digest, "size": items[0]["size"], "keep": items[0], "copies": items[1:]})
        groups.sort(key=lambda group: natural_key(group["keep"]["path"]))
        with self.lock:
            self._groups[key] = groups
        return groups

    def _duplicate_index(self, anchor: str) -> tuple[dict[str, str], dict[str, int]]:
        """Maps each extra copy to the kept path, and each kept path to its number of copies."""
        copy_of: dict[str, str] = {}
        copies: dict[str, int] = {}
        for group in self.duplicate_groups(anchor):
            copies[group["keep"]["path"]] = len(group["copies"])
            for item in group["copies"]:
                copy_of[item["path"]] = group["keep"]["path"]
        return copy_of, copies

    # -- Listing --------------------------------------------------------------------

    def _in_library(self, digest: str | None) -> dict | None:
        known = self.library.by_sha.get(digest or "")
        if not known:
            return None
        shoot = self.library.shoots.get(known[0])
        return {
            "shoot_id": known[0],
            "photo_id": known[1],
            "file": known[2],
            "shoot_name": shoot["name"] if shoot else "",
        }

    def _library_relation(self, current: Path) -> str:
        root = self.library.root
        if root is None or not self.library.online:
            return "other-drive"
        if current == root or current.is_relative_to(root):
            return "inside"
        return "same-drive" if current.anchor.casefold() == root.anchor.casefold() else "other-drive"

    def listing(self, value: str, recursive: bool = False) -> dict:
        """One folder's subfolders and files; ``recursive`` lists the files of every folder below too."""
        current = resolve_directory(value)
        directories: list[dict] = []
        try:
            children = sorted(current.iterdir(), key=lambda child: natural_key(child.name))
        except PermissionError as exc:
            raise KiraError(HTTPStatus.FORBIDDEN, "Windows denied access to this folder") from exc
        for child in children:
            if not _visible(child.name):
                continue
            try:
                if child.is_dir() and (child.name.casefold() not in SKIPPED_SOURCE_DIRS or child.name == LINKS_DIR):
                    # The page hides folder cards while it lists every file below, so skip their covers.
                    card = {} if recursive else _folder_card(child)
                    directories.append({"name": child.name, "path": str(child), **card})
            except OSError:
                continue
        hashes = self._rows_under(current) if recursive else self._rows("folder=?", (str(current),))
        copy_of, copies = self._duplicate_index(current.anchor)
        base = str(current)
        skip = len(base) if base.endswith(os.sep) else len(base) + 1
        files: list[dict] = []
        for path, info in sorted(self._entries(current, recursive), key=lambda item: natural_key(item[0][skip:])):
            row = hashes.get(path)
            digest = (
                row["sha256"] if row and (row["size"], row["mtime_ns"]) == (info.st_size, info.st_mtime_ns) else None
            )
            files.append(
                {
                    "name": os.path.basename(path),
                    "path": path,
                    "folder": os.path.dirname(path[skip:]),
                    "size": info.st_size,
                    "kind": file_kind(path),
                    "checked": digest is not None,
                    "library": self._in_library(digest),
                    "duplicate_of": copy_of.get(path),
                    "copies": copies.get(path, 0),
                }
            )
        groups = self.duplicate_groups(current.anchor)
        media = [item for item in files if item["kind"] in {"photo", "video"}]
        parts = current.parts
        return {
            "current": str(current),
            "parent": None if current.parent == current else str(current.parent),
            "breadcrumbs": [
                {"name": part.rstrip("\\/") or part, "path": str(Path(*parts[: index + 1]))}
                for index, part in enumerate(parts)
            ],
            "directories": directories,
            "files": files,
            "library": self._library_relation(current),
            "recursive": recursive,
            "summary": {
                "files": len(files),
                "checked": sum(item["checked"] for item in files),
                "in_library": sum(bool(item["library"]) for item in media),
                "not_in_library": sum(item["checked"] and not item["library"] for item in media),
                "duplicates": sum(bool(item["duplicate_of"]) for item in files),
            },
            "drive": {
                "anchor": current.anchor,
                "groups": len(groups),
                "copies": sum(len(group["copies"]) for group in groups),
                "copy_bytes": sum(group["size"] * len(group["copies"]) for group in groups),
                "links_folder": str(current / LINKS_DIR),
            },
        }

    def preview(self, value: str, requested_size: int) -> Path:
        path = Path(value)
        if not path.is_file():
            raise KiraError(HTTPStatus.NOT_FOUND, "File not found")
        info = path.stat()
        rows = self._rows("path=?", (str(path),))
        row = rows.get(str(path))
        if row and (row["size"], row["mtime_ns"]) == (info.st_size, info.st_mtime_ns):
            digest = row["sha256"]
        else:  # not hashed yet: key the cached preview by name, size and time instead
            digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{path}|{info.st_size}|{info.st_mtime_ns}").hex
        return self.library._preview_of(path, digest, requested_size)

    # -- Clean-up: shortcuts on another drive ------------------------------------------

    def write_links(self, value: str) -> dict:
        """Write ``<folder>/_trash`` with a shortcut to every extra copy under the folder. Nothing moves.

        Copies are found across the whole drive, so a duplicate whose kept copy sits elsewhere
        on the drive still gets a shortcut.
        """
        current = resolve_directory(value)
        folder = current / LINKS_DIR
        groups = []
        for group in self.duplicate_groups(current.anchor):
            copies = [item for item in group["copies"] if Path(item["path"]).is_relative_to(current)]
            if copies:
                groups.append({**group, "copies": copies})
        links: list[dict] = []
        for group in groups:
            keep = group["keep"]["path"]
            for item in group["copies"]:
                relative = Path(item["path"]).relative_to(current)
                links.append(
                    {
                        "link": str(folder / relative.parent / f"{relative.name}.lnk"),
                        "target": item["path"],
                        "reason": f"Same bytes as {keep}",
                    }
                )
        folder.mkdir(exist_ok=True)
        wanted = {os.path.normcase(link["link"]) for link in links}
        stale = [path for path in folder.rglob("*.lnk") if os.path.normcase(str(path)) not in wanted]
        for path in stale:  # shortcuts from an earlier run whose file is no longer a duplicate
            path.unlink(missing_ok=True)
        for link in links:
            Path(link["link"]).parent.mkdir(parents=True, exist_ok=True)
        _make_shortcuts(links)
        note = [
            f"Duplicates under {current}, found by Kira.",
            "",
            "Each shortcut in this folder points at a file whose bytes are identical (same SHA-256)",
            "to another file on this drive. Nothing was moved or deleted: the files are still where",
            "they were. The copy Kira suggests keeping is the one in the shallowest folder (then the",
            "oldest). Deleting this _trash folder only removes the shortcuts.",
            "",
            f"Written {utc_now()}: {len(groups)} duplicated files, {len(links)} extra copies,"
            f" {sum(g['size'] * len(g['copies']) for g in groups) / 1e9:.2f} GB.",
            "",
        ]
        for group in groups:
            note.append(f"KEEP  {group['keep']['path']}  ({group['size']:,} bytes, SHA-256 {group['sha256']})")
            note.extend(f"  dup {item['path']}" for item in group["copies"])
            note.append("")
        (folder / LINKS_NOTE).write_text("\n".join(note), encoding="utf-8")
        return {"folder": str(folder), "links": len(links), "groups": len(groups), "removed_stale": len(stale)}

    # -- Clean-up: copies of library files on the library drive --------------------------

    def trash_library_copies(self, value: str) -> dict:
        """Move files under ``value`` whose exact bytes are in the library into the library trash."""
        current = resolve_directory(value)
        if self._library_relation(current) != "same-drive":
            raise KiraError(
                HTTPStatus.BAD_REQUEST, "Only folders outside the library, on the library drive, can be cleaned up"
            )
        library = self.library
        root = library._require_online()
        rows = self._current(self._rows_under(current))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        batch = root / TRASH_DIR / stamp / OUTSIDE_DIR
        moved: list[tuple[str, str, str]] = []
        failed: list[dict] = []
        with library.lock:
            for row in sorted(rows, key=lambda item: natural_key(item["path"])):
                known = library.by_sha.get(row["sha256"])
                source = Path(row["path"])
                if not known or not source.is_relative_to(current) or LINKS_DIR in source.parts:
                    continue
                if self._inside_library(source):  # the library's own files are what is being kept
                    continue
                shoot, photo = library.photo(known[0], known[1])
                kept = library.relative_path(shoot, photo, library.file_record(photo, known[2]))
                target = library._free_path(batch / source.parent.relative_to(source.anchor), source.name)
                reason = f"Same bytes as {kept} in the library (SHA-256 {row['sha256']})"
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, target)  # same drive: a rename, so it is instant and reversible
                except OSError as exc:
                    failed.append({"name": source.name, "error": str(exc)})
                    continue
                trash_id = uuid.uuid4().hex[:12]
                library.state.execute(
                    "INSERT INTO trash VALUES (?,?,?,?,?,?)",
                    (trash_id, "", str(source), str(target), row["sha256"], utc_now()),
                )
                library.state.execute("INSERT INTO trash_reasons VALUES (?,?)", (trash_id, reason))
                library.state.execute("DELETE FROM source_hashes WHERE path=?", (str(source),))
                moved.append((str(source), str(target), reason))
        if moved:
            lines = [
                "Files moved here by Kira because the library already holds identical bytes.",
                "To undo, move a file back to its original path. Empty trash erases them for good.",
                "",
            ]
            for source, target, reason in moved:
                lines += [f"{target}", f"  was  {source}", f"  why  {reason}", ""]
            (root / TRASH_DIR / stamp / WHY_NOTE).write_text("\n".join(lines), encoding="utf-8")
        with self.lock:
            self._groups.pop(current.anchor.casefold(), None)
        return {"moved": len(moved), "failed": failed, "trash_folder": str(batch.parent) if moved else ""}


class _Guid(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _Guid:
    value = _Guid()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(value))
    return value


def _com_call(pointer: ctypes.c_void_p, index: int, types: tuple = (), *arguments) -> None:
    """Call method ``index`` of a COM interface; failures raise OSError."""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *types)(table[index])(pointer, *arguments)


_QUERY = (ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))
_TEXT = (ctypes.c_wchar_p,)
_SAVE = (ctypes.c_wchar_p, ctypes.c_int)


def _make_shortcuts(links: list[dict]) -> None:
    """Write a shortcut for each ``{link, target, reason}``; the reason becomes its comment.

    On Windows this asks the shell itself (IShellLinkW) to write a real ``.lnk``; elsewhere
    a symbolic link stands in.
    """
    if os.name != "nt":
        for link in links:
            if not os.path.lexists(link["link"]):
                os.symlink(link["target"], link["link"])
        return
    ole32 = ctypes.oledll.ole32
    try:
        ole32.CoInitialize(None)
    except OSError:
        pass  # this thread already runs COM in another mode, which works for the shell link too
    shell_link, persist = ctypes.c_void_p(), ctypes.c_void_p()
    ole32.CoCreateInstance(
        ctypes.byref(_guid("{00021401-0000-0000-C000-000000000046}")),
        None,
        1,  # CLSCTX_INPROC_SERVER
        ctypes.byref(_guid("{000214F9-0000-0000-C000-000000000046}")),  # IShellLinkW
        ctypes.byref(shell_link),
    )
    try:
        iid_persist_file = _guid("{0000010B-0000-0000-C000-000000000046}")
        _com_call(shell_link, 0, _QUERY, ctypes.byref(iid_persist_file), ctypes.byref(persist))
        try:
            for link in links:
                _com_call(shell_link, 20, _TEXT, link["target"])  # SetPath
                _com_call(shell_link, 7, _TEXT, link["reason"][:250])  # SetDescription
                _com_call(persist, 6, _SAVE, link["link"], 1)  # IPersistFile.Save
        finally:
            _com_call(persist, 2)  # Release
    finally:
        _com_call(shell_link, 2)
