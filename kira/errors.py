from __future__ import annotations

from http import HTTPStatus


class KiraError(Exception):
    def __init__(self, status: int, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra
