''' BlobStore Protocol: record/derived artifact storage, swappable backend. '''

import os
import uuid
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def url(self, key: str) -> str: ...


class LocalBlobStore:
    ''' Writes under a root dir on the local filesystem. '''

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, data: bytes, content_type: str) -> None:
        # content_type unused locally; kept for Protocol parity with S3 impl later
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # write to a temp file in the same dir, then atomic rename onto the final
        # key -> a crash mid-write never leaves a partial/truncated file at `key`
        tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic on POSIX and Windows, same filesystem
        except BaseException:
            tmp.unlink(missing_ok=True) # delete the tmp file on exception/ bad write
            raise

    
    # get the path as
    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    # resolved the path to 'file://' so that non-local callers is compatible.
    def url(self, key: str) -> str:
        return self._path(key).resolve().as_uri()
