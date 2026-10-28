"""Immutable local objects, durable receipts and retryable file publication.

The catalog is local to one Kira desktop. A retained object is a local checkpoint,
not a claim of an independent backup. Transfer jobs are disposable views over it.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .errors import KiraError
from .media import CHUNK_COPY_SIZE, atomic_json_write, sha256_file, utc_now
from http import HTTPStatus


def sync_directory(path: Path) -> None:
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_new(temporary: Path, destination: Path) -> None:
    """Publish on one volume without replacing someone else's existing file."""
    if os.name == 'nt':
        os.rename(temporary, destination)  # Windows rename rejects an existing target.
    else:
        os.link(temporary, destination)
        temporary.unlink()
    sync_directory(destination.parent)


class Preservation:
    def __init__(self, root: Path):
        self.root = root
        self.objects = root / 'objects'
        self.receipts = root / 'receipts'
        self.staging = root / 'staging'
        self.database = root / 'catalog.sqlite3'
        self.lock = threading.RLock()
        for folder in (self.objects, self.receipts, self.staging):
            folder.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] > 1:
                raise KiraError(HTTPStatus.CONFLICT, 'This library was created by a newer Kira version')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS blobs (
                    sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY, job_id TEXT, kind TEXT NOT NULL,
                    record_id TEXT NOT NULL, sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, destination TEXT NOT NULL,
                    status TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS revisions_job ON revisions(job_id);
                PRAGMA user_version = 1;
            ''')
        # Import receipts absent from the database, including a rebuild that was
        # interrupted after only some manifests committed. Existing rows do not
        # require rereading their media on every application startup.
        self.rebuild(missing_only=True)

    @contextmanager
    def transaction(self):
        with self.lock:
            db = sqlite3.connect(self.database, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA foreign_keys=ON')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def object_path(self, digest: str) -> Path:
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise KiraError(HTTPStatus.BAD_REQUEST, 'Invalid content fingerprint')
        return self.objects / digest[:2] / digest

    def verified_object(self, digest: str) -> Path:
        path = self.object_path(digest)
        if not path.is_file() or sha256_file(path) != digest:
            raise KiraError(HTTPStatus.CONFLICT, 'The retained copy is missing or damaged; keep the source file')
        return path

    def retain(self, source: Path, expected: str | None = None) -> dict:
        """Copy stable source bytes into a content-addressed checkpoint and verify them."""
        temporary = self.staging / (uuid.uuid4().hex + '.part')
        try:
            before = source.stat()
            digest = hashlib.sha256()
            size = 0
            with source.open('rb') as incoming, temporary.open('xb') as outgoing:
                while chunk := incoming.read(CHUNK_COPY_SIZE):
                    digest.update(chunk)
                    size += len(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ) or size != before.st_size:
                raise KiraError(HTTPStatus.CONFLICT, 'The source changed while saving; finish editing and retry')
            value = digest.hexdigest()
            if expected and value != expected:
                raise KiraError(HTTPStatus.CONFLICT, 'File checksum does not match; the source has changed')
            if sha256_file(temporary) != value:
                raise KiraError(HTTPStatus.CONFLICT, 'The saved copy could not be verified')
            target = self.object_path(value)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                publish_new(temporary, target)
            except FileExistsError:
                self.verified_object(value)
            with self.transaction() as db:
                db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?,?)', (value, size, utc_now()))
            return {'sha256': value, 'size': size, 'path': str(target)}
        finally:
            temporary.unlink(missing_ok=True)

    def publish(self, operation_id: str, digest: str, destination: Path) -> Path:
        """Copy a retained object through destination-side staging, then commit its receipt.

        The planned destination is recorded before any external writes. Retrying an
        interrupted operation verifies its exact target instead of inventing __v2.
        """
        with self.lock:
            with self.transaction() as db:
                old = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
                if old:
                    if old['sha256'] != digest or old['destination'] != str(destination):
                        raise KiraError(HTTPStatus.CONFLICT, 'This operation belongs to a different file')
                else:
                    db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                               (operation_id, digest, str(destination), 'pending', utc_now()))
            source = self.verified_object(digest)
            if destination.exists():
                if not destination.is_file() or sha256_file(destination) != digest:
                    raise KiraError(HTTPStatus.CONFLICT, 'The destination changed; the retained copy is safe')
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                token = hashlib.sha256(operation_id.encode()).hexdigest()[:24]
                temporary = destination.parent / ('.kira-' + token + '.part')
                try:
                    # A previous interrupted copy may exist. Only this operation owns it.
                    with source.open('rb') as incoming, temporary.open('wb') as outgoing:
                        while chunk := incoming.read(CHUNK_COPY_SIZE):
                            outgoing.write(chunk)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if sha256_file(temporary) != digest:
                        raise KiraError(HTTPStatus.CONFLICT, 'The destination copy could not be verified')
                    publish_new(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            with self.transaction() as db:
                db.execute('UPDATE operations SET status=?, updated_at=? WHERE id=?',
                           ('complete', utc_now(), operation_id))
            return destination

    def move(self, operation_id: str, digest: str, source: Path, destination: Path) -> Path:
        """Journal an organization move without deleting a newly edited source.

        Move the actual file rather than unlinking it after copying. If an editor
        changes that file during the operation, its bytes remain at the destination
        and the immutable checkpoint still contains the earlier bytes.
        """
        with self.lock:
            if source.resolve() == destination.resolve() or destination.exists() or not source.exists():
                return self.publish(operation_id, digest, destination)
            self.verified_object(digest)
            with self.transaction() as db:
                previous = db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
                if previous and (previous['sha256'] != digest or previous['destination'] != str(destination)):
                    raise KiraError(HTTPStatus.CONFLICT, 'Organization destination changed')
                if not previous:
                    db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                               (operation_id,digest,str(destination),'pending',utc_now()))
            destination.parent.mkdir(parents=True,exist_ok=True)
            publish_new(source,destination)
            sync_directory(source.parent)
            if sha256_file(destination) != digest:
                raise KiraError(HTTPStatus.CONFLICT,
                                f'Source changed during organization; its current bytes are kept at {destination}')
            with self.transaction() as db:
                db.execute('UPDATE operations SET status=?,updated_at=? WHERE id=?',
                           ('complete',utc_now(),operation_id))
            return destination

    def receipt(self, job_id: str, kind: str, record: dict) -> str:
        digest = record['sha256']
        verified = self.verified_object(digest)
        size = verified.stat().st_size
        if size != record['size']:
            raise KiraError(HTTPStatus.CONFLICT, 'The saved record does not match the retained file size')
        key = f"{job_id}:{kind}:{record['id']}:{digest}"
        revision_id = hashlib.sha256(key.encode()).hexdigest()[:32]
        payload = {'schema': 1, 'id': revision_id, 'job_id': job_id, 'kind': kind,
                   'record': record, 'created_at': utc_now(), 'protection': 'local_checkpoint'}
        receipt_path = self.receipts / (revision_id + '.json')
        with self.lock:
            # Immutable manifests can rebuild the catalog if the database is lost.
            if not receipt_path.exists():
                atomic_json_write(receipt_path, payload)
                sync_directory(receipt_path.parent)
            else:
                payload = json.loads(receipt_path.read_text(encoding='utf-8'))
            with self.transaction() as db:
                db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?,?)',
                           (digest,size,payload['created_at']))
                db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?,?,?,?,?)',
                           (revision_id, job_id, kind, record['id'], digest,
                            json.dumps(payload), payload['created_at']))
        return revision_id

    def history(self, job_id: str | None = None, *, limit: int | None = None, offset: int = 0) -> list[dict]:
        with self.transaction() as db:
            sql = ('SELECT metadata FROM revisions' + (' WHERE job_id=?' if job_id else '')
                   + ' ORDER BY created_at DESC, id DESC')
            params = (job_id,) if job_id else ()
            if limit is not None:
                sql += ' LIMIT ? OFFSET ?'
                params += (limit, offset)
            rows = db.execute(sql, params).fetchall()
        return [json.loads(row['metadata']) for row in rows]

    def revision(self, revision_id: str) -> dict:
        with self.transaction() as db:
            row = db.execute('SELECT metadata FROM revisions WHERE id=?', (revision_id,)).fetchone()
        if not row:
            raise KiraError(HTTPStatus.NOT_FOUND, 'Saved revision not found')
        return json.loads(row['metadata'])

    def operation(self, operation_id: str) -> dict | None:
        with self.transaction() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        return dict(row) if row else None

    def restore(self, revision_id: str, destination: Path) -> dict:
        saved = self.revision(revision_id)
        digest = saved['record']['sha256']
        self.publish('restore:' + uuid.uuid4().hex, digest, destination)
        return {'path': str(destination), 'sha256': digest, 'revision_id': revision_id}

    def rebuild(self, *, missing_only: bool = False) -> int:
        count = 0
        with self.transaction() as db:
            known = {row[0] for row in db.execute('SELECT id FROM revisions')}
        for path in self.receipts.glob('*.json'):
            if missing_only and path.stem in known:
                continue
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.receipt(saved['job_id'], saved['kind'], saved['record'])
            count += 1
        return count
