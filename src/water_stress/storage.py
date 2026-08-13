from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol


class StorageClient(Protocol):
    def exists(self, path: Path) -> bool: ...

    def read_bytes(self, path: Path) -> bytes: ...

    def write_bytes(self, path: Path, content: bytes) -> None: ...

    def write_chunks(self, path: Path, chunks: Iterable[bytes]) -> tuple[str, int, bytes]: ...

    def checksum(self, path: Path) -> str: ...

    def delete(self, path: Path) -> None: ...


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

    def write_chunks(self, path: Path, chunks: Iterable[bytes]) -> tuple[str, int, bytes]:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        digest = hashlib.sha256()
        size = 0
        header = b""
        try:
            with temporary_path.open("wb") as output:
                for chunk in chunks:
                    if not chunk:
                        continue
                    if len(header) < 8:
                        header += chunk[: 8 - len(header)]
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            temporary_path.replace(path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return digest.hexdigest(), size, header

    def checksum(self, path: Path) -> str:
        return hashlib.sha256(self.read_bytes(path)).hexdigest()

    def delete(self, path: Path) -> None:
        path.unlink(missing_ok=True)
