from dataclasses import dataclass
from typing import Any


@dataclass
class ApiResponse:
    rpcid: str
    data: Any
    success: bool
    response_id: str


@dataclass
class DriveMedia:
    media_key: str
    mime_type: str
