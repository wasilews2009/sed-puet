"""Адаптер MinIO з підміненим клієнтом `minio.Minio`."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from minio.error import S3Error
from minio.sse import SseS3

from sed_ingest.adapters.minio_storage import MinioObjectStorage
from sed_ingest.domain.models import FileKind, IncomingFile, StorageUnavailable, StoredObject

DATA = b"%PDF-1.7 test"
FILE = IncomingFile(DATA, "nakaz.pdf", FileKind.PDF, hashlib.sha256(DATA).hexdigest())


class FakeMinio:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.put_calls: list[dict[str, Any]] = []
        self.removed: list[tuple[str, str, str | None]] = []
        self.buckets: set[str] = set()
        self.versioning: dict[str, Any] = {}

    def bucket_exists(self, b: str) -> bool:
        return b in self.buckets

    def make_bucket(self, b: str) -> None:
        self.buckets.add(b)

    def set_bucket_versioning(self, b: str, cfg: Any) -> None:
        self.versioning[b] = cfg.status

    def put_object(self, bucket, key, data, length, content_type, metadata, sse):  # type: ignore[no-untyped-def]
        if self.fail:
            raise S3Error(None, "InternalError", "boom", "r", "id", "host")  # type: ignore[arg-type]
        self.put_calls.append(dict(bucket=bucket, key=key, body=data.read(), length=length,
                                   content_type=content_type, metadata=metadata, sse=sse))
        return SimpleNamespace(version_id="ver-1", etag="etag-1")

    def remove_object(self, bucket: str, key: str, version_id: str | None = None) -> None:
        self.removed.append((bucket, key, version_id))


async def test_put_stores_bytes_with_hash_and_encryption() -> None:
    client = FakeMinio()
    storage = MinioObjectStorage(client, "sed-documents", sse=True)  # type: ignore[arg-type]
    storage.ensure_bucket()
    obj = await storage.put("documents/d/v1/x.pdf", FILE, {"document-id": "d"})
    call = client.put_calls[0]
    assert obj == StoredObject("sed-documents", "documents/d/v1/x.pdf", "ver-1", "etag-1")
    assert call["body"] == DATA and call["length"] == len(DATA) and call["content_type"] == "application/pdf"
    assert call["metadata"] == {"sha256": FILE.sha256, "document-id": "d"}
    assert isinstance(call["sse"], SseS3)
    assert client.versioning == {"sed-documents": "Enabled"}


async def test_put_failure_is_reported_as_unavailable() -> None:
    storage = MinioObjectStorage(FakeMinio(fail=True), "b", sse=False)  # type: ignore[arg-type]
    with pytest.raises(StorageUnavailable):
        await storage.put("k", FILE, {})


async def test_delete_removes_exact_version() -> None:
    client = FakeMinio()
    await MinioObjectStorage(client, "b").delete(StoredObject("b", "k", "ver-1", None))  # type: ignore[arg-type]
    assert client.removed == [("b", "k", "ver-1")]
