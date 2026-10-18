"""Читання оригіналів документів з MinIO для віддачі в браузер.

Браузер не отримує прямих (presigned) посилань на сховище: файл іде через застосунок, тож
кожне завантаження проходить перевірку доступу і потрапляє в журнал аудиту.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from minio import Minio
from minio.error import S3Error

from ..domain.errors import ModuleUnavailable, NotFound


class FileReader(Protocol):
    def open(self, bucket: str, key: str, version_id: str | None) -> Iterator[bytes]:
        """Ітератор шматків файлу; кидає `NotFound` / `ModuleUnavailable`."""
        ...


class MinioFileReader:
    def __init__(self, client: Minio, chunk_bytes: int = 256 * 1024) -> None:
        self.client = client
        self.chunk_bytes = chunk_bytes

    def open(self, bucket: str, key: str, version_id: str | None) -> Iterator[bytes]:
        try:
            resp = self.client.get_object(bucket, key, version_id=version_id)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchVersion", "NoSuchBucket"):
                raise NotFound("Файл версії відсутній у сховищі") from exc
            raise ModuleUnavailable("Сховище файлів недоступне") from exc
        except Exception as exc:  # мережеві помилки urllib3
            raise ModuleUnavailable("Сховище файлів недоступне") from exc

        def chunks() -> Iterator[bytes]:
            try:
                yield from resp.stream(self.chunk_bytes)
            finally:
                resp.close()
                resp.release_conn()

        return chunks()
