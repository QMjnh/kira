"""Move photo folders from before the shoot library into ``<year>/<name>/`` shoots.

Run from the repository root while Kira is stopped:
    python scripts/migrate_old_folders.py            # list what would happen
    python scripts/migrate_old_folders.py --apply    # do it

A folder with photos directly inside becomes one shoot, subfolders included; a folder
without any (``iphone/``) is looked into instead. Files are moved on the same drive and
filed like an import: duplicates go to ``_trash``, unsure matches wait in review, files
Kira does not handle stay where they were. Running it again continues where it stopped.
Folders with the same year and name merge into one shoot.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kira.library import REJECT_SOURCE_DIRS, SAVE_EVERY, SKIPPED_SOURCE_DIRS, Library
from kira.media import clean_folder_name, natural_key, sha256_file
from kira.metadata import file_kind
from kira.tasks import ordered_parallel

YEAR_PREFIX = re.compile(r"((?:19|20)\d{2})[\s_-]*(.*)")
PHONE_MONTH = re.compile(r"(\d{4})(\d{2})_([a-z])", re.IGNORECASE)
MEDIA = {"photo", "video"}
HASH_WORKERS = 4


def old_folders(library: Library) -> list[tuple[Path, str, str]]:
    """(folder, year, name) for every folder that holds photos but no shoot manifest."""
    managed = [shoot["path"] for shoot in library.shoots.values()]
    found = []

    def visit(folder: Path, parts: list[str]) -> None:
        if folder in managed:
            return
        entries = sorted(folder.iterdir(), key=lambda entry: natural_key(entry.name))
        holds_shoot = any(path.is_relative_to(folder) for path in managed)
        if parts and not holds_shoot and any(entry.is_file() and file_kind(entry) in MEDIA for entry in entries):
            found.append((folder, *identity(folder, parts)))
            return
        for entry in entries:
            if (
                entry.is_dir()
                and not entry.name.startswith(("_", "."))
                and entry.name.casefold() not in SKIPPED_SOURCE_DIRS
            ):
                visit(entry, [*parts, entry.name])

    visit(library.root, [])
    return found


def identity(folder: Path, parts: list[str]) -> tuple[str, str]:
    """``2026_boston`` -> (2026, Boston); ``iphone/202607_b`` -> (2026, 2026-07 iPhone 2)."""
    month = PHONE_MONTH.fullmatch(parts[-1])
    if len(parts) > 1 and parts[-2].casefold() == "iphone" and month:
        copy = ord(month[3].casefold()) - ord("a") + 1
        return month[1], f"{month[1]}-{month[2]} iPhone" + (f" {copy}" if copy > 1 else "")
    stripped = [
        (match[1], match[2].strip(" _-")) if (match := YEAR_PREFIX.fullmatch(part)) else ("", part) for part in parts
    ]
    year = next((found for found, _ in reversed(stripped) if found), "") or oldest_year(folder)
    name = stripped[-1][1]
    if not name[:1].isalpha():
        name = parts[-1] if len(parts) == 1 else f"{stripped[-2][1] or parts[-2]} {parts[-1]}"
    name = name.upper() if len(name) <= 3 and name.islower() else name[:1].upper() + name[1:]
    return year, clean_folder_name(name)


def oldest_year(folder: Path) -> str:
    stamps = [path.stat().st_mtime for path in folder.rglob("*") if path.is_file() and file_kind(path) in MEDIA]
    return str(datetime.fromtimestamp(min(stamps)).year if stamps else datetime.now().year)


def migrate(library: Library, folder: Path, year: str, name: str) -> dict:
    # Folders with the same year and name (e.g. two copies of one trip) merge into one shoot.
    wanted = (year, name.casefold())
    shoot = next((item for item in library.shoots.values() if (item["year"], item["name"].casefold()) == wanted), None)
    if shoot:
        library.rescan(shoot["id"])  # picks up files moved just before an interrupted run saved
    shoot = shoot or library.create_shoot(name, year)
    files = library._source_files(folder)
    counts: dict[str, int] = {}
    failed = []
    batch: dict = {}
    touched = {shoot["id"]}
    # The USB drive is the bottleneck: hash upcoming files on worker threads while this one files the last.
    hashed = ordered_parallel(files, sha256_file, workers=HASH_WORKERS)
    for index, (path, digest, error) in enumerate(hashed, start=1):
        rejected = any(part.casefold() in REJECT_SOURCE_DIRS for part in path.relative_to(folder).parts[:-1])
        try:
            if error:
                raise error
            result = library.ingest_file(
                path, shoot["id"], source_kind="folder", in_place=True, rejected=rejected, batch=batch, digest=digest
            )
            touched.add(result.get("shoot_id") or shoot["id"])
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        except Exception as exc:  # noqa: BLE001 - reported, the file stays where it was
            failed.append(f"{path}: {getattr(exc, 'message', exc)}")
        print(f"\r  {index}/{len(files)}", end="", flush=True)
        if index % SAVE_EVERY == 0:
            library._flush(touched)
    library._flush(touched)
    print()
    for directory, _, _ in sorted(os.walk(folder), key=lambda row: len(row[0]), reverse=True):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    for parent in folder.parents:  # e.g. iphone/ once its last month moved out
        if parent == library.root:
            break
        try:
            parent.rmdir()
        except OSError:
            break
    return {
        "folder": folder.relative_to(library.root).as_posix(),
        "shoot": f"{shoot['year']}/{shoot['name']}",
        "counts": counts,
        "failed": failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[1] / "kira-data")
    parser.add_argument("--apply", action="store_true", help="move the files; without it only list the plan")
    args = parser.parse_args()
    config = json.loads((args.data / "config.json").read_text(encoding="utf-8"))
    library = Library(args.data, Path(config["library_root"]))
    try:
        plan = old_folders(library)
        for folder, year, name in plan:
            print(f"{folder.relative_to(library.root).as_posix()}  ->  {year}/{name}")
        if not args.apply:
            print(f"\n{len(plan)} folders. Run again with --apply to move them.")
            return
        for folder, year, name in plan:
            print(f"{folder.relative_to(library.root).as_posix()}  ->  {year}/{name}")
            report = migrate(library, folder, year, name)
            print(f"  {report['shoot']}: {report['counts']}")
            for line in report["failed"]:
                print(f"  FAILED {line}")
            if folder.exists():
                print(f"  left behind in {folder}: files Kira does not handle")
    finally:
        library.close()


if __name__ == "__main__":
    main()
