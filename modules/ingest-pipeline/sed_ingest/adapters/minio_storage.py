"""Сховище оригіналів у MinIO (S3-сумісне, on-premise).

* Бакет з увімкненим versioning: кожен об'єкт має `version_id`, що пишеться в
  `document_versions.storage_version_id` — підміна файлу «поверх» не стирає оригінал.
* SSE-S3 (шифрування на диску через KMS MinIO), якщо `sse=True`.
* У метаданих об'єкта — SHA-256 вмісту: незалежна від БД перевірка цілісності.
* Клієнт `minio` синхронний, тому виклики йдуть у пул потоків.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import anyio
from minio import Minio
from minio.commonconfig import ENABLED
from minio.error import S3Error
from minio.sse import SseS3
from minio.versioningconfig import VersioningConfig

from ..domain.models import IncomingFile, StorageUnavailable, StoredObject

log = logging.getLogger(__name__)


class MinioObjectStorage:
    """Реалізація порту `ObjectStorage`."""

    def __init__(self, client: Minio, bucket: str, *, sse: bool = True) -> None:
        self.client = client
        self.bucket = bucket
        self.sse = SseS3() if sse else None

    def ensure_bucket(self) -> None:
        """Викликається при старті застосунку."""
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
        self.client.set_bucket_versioning(self.bucket, VersioningConfig(ENABLED))

    async def put(self, key: str, file: IncomingFile, metadata: dict[str, str]) -> StoredObject:
        meta: dict[str, Any] = {"sha256": file.sha256, **metadata}
        try:
            result = await anyio.to_thread.run_sync(lambda: self.client.put_object(
                self.bucket, key, io.BytesIO(file.data), length=file.size,
                content_type=file.mime_type, metadata=meta, sse=self.sse,
            ))
        except (S3Error, OSError) as exc:
            log.error("MinIO put failed: %s", type(exc).__name__)
            raise StorageUnavailable("Сховище файлів недоступне, спробуйте пізніше") from exc
        return StoredObject(bucket=self.bucket, key=key, version_id=result.version_id, etag=result.etag)

    async def delete(self, obj: StoredObject) -> None:
        try:
            await anyio.to_thread.run_sync(
                lambda: self.client.remove_object(obj.bucket, obj.key, version_id=obj.version_id))
        except (S3Error, OSError):
            # Не маскуємо первинну помилку; осиротілий об'єкт прибере lifecycle-правило / звірка.
            log.exception("MinIO compensation delete failed for %s", obj.key)
