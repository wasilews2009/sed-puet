"""Сховища контейнерів КЕП: MinIO (продакшн) і в пам'яті (тести, локальний запуск)."""

from __future__ import annotations

import io

import anyio
from minio import Minio
from minio.sse import SseS3


class MinioSignatureStore:
    """Бакет варто створювати з versioning + object lock (COMPLIANCE): підпис не можна стерти."""

    def __init__(self, client: Minio, bucket: str, *, sse: bool = True) -> None:
        self.client, self.bucket, self._sse = client, bucket, SseS3() if sse else None

    async def put(self, key: str, data: bytes, metadata: dict[str, str]) -> None:
        await anyio.to_thread.run_sync(lambda: self.client.put_object(
            self.bucket, key, io.BytesIO(data), len(data), content_type="application/pkcs7-signature",
            metadata=metadata, sse=self._sse))

    async def delete(self, key: str) -> None:
        await anyio.to_thread.run_sync(self.client.remove_object, self.bucket, key)


class MinioContentReader:
    """Читає файл версії документа (бакет документів, з урахуванням версії об'єкта S3)."""

    def __init__(self, client: Minio, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        self.client, self.max_bytes = client, max_bytes

    async def read(self, bucket: str, key: str, version_id: str | None) -> bytes:
        def _get() -> bytes:
            resp = self.client.get_object(bucket, key, version_id=version_id)
            try:
                data = resp.read(self.max_bytes + 1)
            finally:
                resp.close()
                resp.release_conn()
            if len(data) > self.max_bytes:
                raise ValueError("Файл версії завеликий для перевірки підпису ДСТУ")
            return data
        return await anyio.to_thread.run_sync(_get)


class InMemoryContentReader:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def read(self, bucket: str, key: str, version_id: str | None) -> bytes:
        return self.objects[(bucket, key)]


class InMemorySignatureStore:
    def __init__(self, bucket: str = "sed-signatures") -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    async def put(self, key: str, data: bytes, metadata: dict[str, str]) -> None:
        if key in self.objects:
            raise FileExistsError(key)
        self.objects[key] = (data, metadata)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
