"""Hold one desktop writer per working directory, released by the OS on exit."""
from pathlib import Path
import os
import threading
from http import HTTPStatus

from .errors import KiraError


class WriterLease:
    def __init__(self, root: Path):
        root.mkdir(parents=True,exist_ok=True)
        self.handle = (root / '.writer.lock').open('a+b')
        self.lock = threading.Lock()
        self.pending = 0
        self.closed = False
        try:
            self.handle.seek(0,2)
            if self.handle.tell() == 0:
                self.handle.write(b'0')
                self.handle.flush()
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise KiraError(HTTPStatus.CONFLICT,'Another Kira is already using this working folder; stop it first') from exc

    def close(self):
        with self.lock:
            self.closed = True
            if not self.pending and not self.handle.closed:
                self.handle.close()

    def retain_request(self):
        with self.lock:
            if self.closed:
                raise KiraError(HTTPStatus.SERVICE_UNAVAILABLE,'Kira is stopping; retry after restart')
            self.pending += 1

    def release_request(self):
        with self.lock:
            self.pending -= 1
            if self.closed and not self.pending and not self.handle.closed:
                self.handle.close()
