"""Verified, journaled source organization and non-destructive archive handling."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile

from .errors import KiraError
from .media import (CHUNK_COPY_SIZE, atomic_json_write, resolve_directory, safe_filename,
                    unique_destination, utc_now)

MAX_ARCHIVE_MEMBERS = 50000
MAX_EXPANDED_BYTES = 100 * 1024**3


def component_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def organize(store, job_id: str) -> dict:
    with store.lock:
        manifest = store._load_manifest(job_id)
        if not manifest.get('source_directory'):
            from http import HTTPStatus
            raise KiraError(HTTPStatus.BAD_REQUEST, 'Job has no source folder')
        root = resolve_directory(manifest['source_directory'])
        folders = {'raw': root / 'selected/raw', 'unselected_jpeg': root / 'unselected_jpeg',
                   'pre_edit': root / 'selected/pre-edit', 'edited': root / 'selected'}
        for folder in folders.values():
            if not folder.resolve().is_relative_to(root.resolve()):
                from http import HTTPStatus
                raise KiraError(HTTPStatus.CONFLICT, 'Organization destination points outside the source folder')
            folder.mkdir(parents=True, exist_ok=True)
        moved, copied, errors = [], [], []
        moved_paths = {}
        component_digests = {}
        component_sizes = {}
        records_by_source = {r['source_path']: r for r in manifest['files']}
        # Stable component IDs survive retries even when a move committed before
        # the job manifest. The operation journal supplies the committed target.
        for asset in manifest.get('assets', []):
            for kind in ('raw_files', 'jpeg_files', 'video_files', 'other_files'):
                for item in asset.get(kind, []):
                    item.setdefault('id', component_id(job_id + ':' + item['path']))
        store._save_manifest(manifest)

        def move_component(item, folder):
            source = Path(item['path'])
            opid = f'organize:{job_id}:{item["id"]}'
            previous = store.preservation.operation(opid)
            try:
                if previous:
                    destination = Path(previous['destination'])
                    digest = previous['sha256']
                    store.preservation.move(opid, digest, source, destination)
                else:
                    if not source.is_file():
                        raise OSError(f'Missing source file: {source}; cannot adopt a same-name file')
                    original = records_by_source.get(str(source))
                    expected = original.get('sha256') if original else item.get('sha256')
                    retained = store.preservation.retain(source, expected)
                    digest = retained['sha256']
                    destination = (source if source.parent.resolve() == folder.resolve()
                                   else unique_destination(folder, item['filename']))
                    receipt = dict(item, sha256=digest, size=retained['size'],
                                   storage_path=str(destination), original_filename=item['filename'])
                    store.preservation.receipt(job_id, 'components', receipt)
                    store.preservation.move(opid, digest, source, destination)
                if source.resolve() != destination.resolve():
                    moved.append({'from':str(source), 'to':str(destination), 'sha256':digest})
                moved_paths[str(source)] = str(destination)
                component_digests[str(source)] = digest
                size = store.preservation.object_path(digest).stat().st_size
                component_sizes[str(source)] = size
                item.update(path=str(destination), filename=destination.name, sha256=digest, size=size)
            except (OSError, KiraError) as exc:
                errors.append(str(exc))

        for asset in manifest.get('assets', []):
            for item in asset.get('raw_files', []):
                move_component(item, folders['raw'])
            destination = folders['pre_edit'] if asset.get('selected') else folders['unselected_jpeg']
            for kind in ('jpeg_files', 'other_files'):
                for item in asset.get(kind, []):
                    move_component(item, destination)
            # Videos participate in the checkpoint and move together with their group.
            for item in asset.get('video_files', []):
                move_component(item, destination)
        for record in manifest['files']:
            source = record['source_path']
            if source in moved_paths:
                record.update(source_path=moved_paths[source], filename=Path(moved_paths[source]).name,
                              sha256=component_digests[source], size=component_sizes[source])
                record['revision_id'] = store.preservation.receipt(job_id, 'files', record)

        for record in manifest['returns']:
            try:
                digest = record.get('sha256')
                if not digest:
                    retained = store.preservation.retain(Path(record['storage_path']))
                    record.update(sha256=retained['sha256'], size=retained['size'])
                elif not store.preservation.object_path(digest).exists():
                    store.preservation.retain(Path(record['storage_path']), digest)
                saved = store.preservation.verified_object(record['sha256'])
                store.preservation.receipt(job_id, 'returns', record)
                if Path(record['original_filename']).suffix.lower() == '.zip':
                    paths = extract_archive(store, manifest, record, saved, folders['edited'], copied)
                    record['organized_paths'] = paths
                else:
                    operation = f'organize-return:{job_id}:{record["id"]}'
                    previous = store.preservation.operation(operation)
                    source = Path(record['storage_path'])
                    destination = (Path(previous['destination']) if previous else
                                   source if source.parent.resolve() == folders['edited'].resolve() else
                                   unique_destination(folders['edited'], record['filename']))
                    store.preservation.publish(operation, record['sha256'], destination)
                    record.update(storage_path=str(destination), organized_paths=[str(destination)])
                    copied.append({'from':str(saved), 'to':str(destination), 'sha256':record['sha256']})
            except (OSError, KiraError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
                errors.append(f'Could not organize {record["filename"]}: {exc}')
        completed = utc_now()
        report_path = root / f'KIRA-ORGANIZED-{job_id}.json'
        report = {'schema':2, 'kira_job_id':job_id, 'job_name':manifest['name'],
                  'completed_at':completed, 'source_directory':str(root),
                  'folders':{k:str(v) for k,v in folders.items()}, 'moved':moved,
                  'copied':copied, 'errors':errors,
                  'files':manifest['files'], 'assets':manifest.get('assets',[]),
                  'returns':manifest['returns']}
        atomic_json_write(report_path, report)
        result = {'status':'complete_with_warnings' if errors else 'complete',
                  'layout_version':3,
                  'completed_at':completed, 'source_directory':str(root),
                  'report_path':str(report_path), 'moved_count':len(moved),
                  'copied_count':len(copied), 'errors':errors}
        manifest['postprocess'] = result
        store._save_manifest(manifest)
        return result


def extract_archive(store, manifest, record, saved: Path, destination: Path, copied: list) -> list[str]:
    """Verify and checkpoint every member before publishing any of this archive."""
    job_id = manifest['id']
    container = destination / (safe_filename(Path(record['original_filename']).stem) + '-' + record['id'][:8])
    members = record.setdefault('members', [])
    existing = {m['member_index']:m for m in members}
    with zipfile.ZipFile(saved) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_MEMBERS or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
            raise ValueError('Archive exceeds the supported extraction size or member count; original retained')
        for entry in entries:
            path = PurePosixPath(entry.filename.replace('\\','/'))
            mode = entry.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or any(':' in part for part in path.parts)
                    or '\0' in entry.filename or stat.S_ISLNK(mode)):
                raise ValueError('Unsafe archive path; original archive retained')
        for index, entry in enumerate(entries):
            if entry.is_dir():
                continue
            member = existing.get(index)
            if member:
                store.preservation.verified_object(member['sha256'])
                continue
            part = store.preservation.staging / ('zip-' + component_id(job_id + record['id'] + str(index)) + '.part')
            try:
                with archive.open(entry) as incoming, part.open('wb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=CHUNK_COPY_SIZE)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                retained = store.preservation.retain(part)
            finally:
                part.unlink(missing_ok=True)
            relative = Path(*[safe_filename(p) for p in PurePosixPath(entry.filename.replace('\\','/')).parts])
            target_folder = container / relative.parent
            if not target_folder.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Archive destination escapes the selected folder; archive retained')
            target_folder.mkdir(parents=True, exist_ok=True)
            target = unique_destination(target_folder, relative.name)
            reserved = {m['storage_path'].casefold() for m in members}
            variant = 2
            while str(target).casefold() in reserved:
                target = target_folder / f'{relative.stem}__v{variant}{relative.suffix}'
                variant += 1
            member = {'id':component_id(record['id'] + ':' + str(index)),
                      'member_index':index, 'archive_member':entry.filename,
                      'archive_sha256':record['sha256'], 'filename':target.name,
                      'original_filename':relative.name, 'size':retained['size'],
                      'sha256':retained['sha256'], 'storage_path':str(target)}
            store._attach_return_match(manifest, member)
            store.preservation.receipt(job_id, 'members', member)
            members.append(member)
            store._save_manifest(manifest)
    paths = []
    for member in members:
        target = Path(member['storage_path'])
        store.preservation.publish(f'zip:{job_id}:{record["id"]}:{member["id"]}',member['sha256'],target)
        paths.append(str(target))
        copied.append({'from':f'{saved}!/{member["archive_member"]}', 'to':str(target),
                       'sha256':member['sha256']})
    # The received archive remains both in its original destination and as a
    # retained object. Unsupported members are opaque components, never dropped.
    return paths
