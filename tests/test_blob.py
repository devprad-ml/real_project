from pathlib import Path

import pytest

from app.storage.blob import LocalBlobStore


def test_put_get_roundtrip(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    store.put("raw/abc/file.pdf", b"hello world", "application/pdf")

    assert store.get("raw/abc/file.pdf") == b"hello world"
    expected = (tmp_path / "raw/abc/file.pdf").resolve().as_uri()
    assert store.url("raw/abc/file.pdf") == expected


# scenario: file is written successfully and maintained atomicity
def test_put_overwrites_atomically(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    store.put("raw/abc/file.pdf", b"first", "application/pdf")
    store.put("raw/abc/file.pdf", b"second", "application/pdf")

    assert store.get("raw/abc/file.pdf") == b"second"
    # no leftover temp files after a clean write
    leftovers = list((tmp_path / "raw/abc").glob(".*.tmp-*"))
    assert leftovers == []

# scenario: missing key raised a filenotfound error

def test_get_missing_key_raises(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        store.get("raw/nope/file.pdf")

# scenario: 
def test_failed_write_leaves_no_partial_file(tmp_path, monkeypatch):
    store = LocalBlobStore(str(tmp_path))
    key = "raw/abc/file.pdf"

    real_replace = __import__("os").replace

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        store.put(key, b"data", "application/pdf")

    # final key was never created, and the temp file was cleaned up
    assert not (tmp_path / key).exists()
    assert list((tmp_path / "raw/abc").glob(".*.tmp-*")) == []
    monkeypatch.setattr("os.replace", real_replace)


def test_the_raw_archive_cannot_be_deleted(tmp_path):
    """raw/ is the record: the code has no business removing it (m13.md 3.6)."""
    store = LocalBlobStore(str(tmp_path))
    store.put("raw/t/sha/claim.pdf", b"the original", "application/pdf")

    with pytest.raises(PermissionError, match="archive"):
        store.delete("raw/t/sha/claim.pdf")

    assert store.get("raw/t/sha/claim.pdf") == b"the original"


def test_a_derived_copy_can_be_deleted(tmp_path):
    """Reclassify re-files the normalized PDF and removes the old key."""
    store = LocalBlobStore(str(tmp_path))
    store.put("derived/t/d/unknown.pdf", b"copy", "application/pdf")

    store.delete("derived/t/d/unknown.pdf")
    store.delete("derived/t/d/unknown.pdf")  # a replayed delete is a no-op

    with pytest.raises(FileNotFoundError):
        store.get("derived/t/d/unknown.pdf")
