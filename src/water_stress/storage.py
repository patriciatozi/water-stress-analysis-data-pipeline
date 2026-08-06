from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol


class StorageClient(Protocol):
    def exists(self, path: Path) -> bool: ...

    def read_bytes(self, path: Path) -> bytes: ...

    def write_bytes(self, path: Path, content: bytes) -> None: ...

    def checksum(self, path: Path) -> str: ...


class LocalStorageClient:
    def exists(self, path: Path) -> bool:
        return path.is_file()

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def write_bytes(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary_path.write_bytes(content)
        temporary_path.replace(path)

    def checksum(self, path: Path) -> str:
        return hashlib.sha256(self.read_bytes(path)).hexdigest()
