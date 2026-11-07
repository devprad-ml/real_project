''' BlobStore Protocol: record/derived artifact storage, swappable backend. '''

import os
import uuid
from pathlib import Path
from typing import Protocol

from app.enums import DocumentType


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def url(self, key: str) -> str: ...


def normalized_key(
    tenant_id: uuid.UUID, document_id: uuid.UUID, doc_type: DocumentType
) -> str:
    ''' Where a document's normalized PDF lives. The file is named for its type
    (`claims.pdf`); the per-document folder keeps a tenant's thousands of claims from
    overwriting each other. Before classification the type is UNKNOWN: `unknown.pdf`. '''
    return f"derived/{tenant_id}/{document_id}/{doc_type.value}.pdf"


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

    def delete(self, key: str) -> None:
        if key.startswith("raw/"):
            # The record. Nothing in the app deletes it; this makes that a rule, not a
            # habit. The production equivalent is a WORM policy on the container.
            raise PermissionError(f"raw/ is the archive, refusing to delete {key!r}")
        # missing_ok: a replayed delete is a no-op, same as S3's DeleteObject.
        self._path(key).unlink(missing_ok=True)

    # resolved the path to 'file://' so that non-local callers is compatible.
    def url(self, key: str) -> str:
        return self._path(key).resolve().as_uri()
