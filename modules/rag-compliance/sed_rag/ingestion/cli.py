"""Командний рядок для нормативного корпусу.

    python -m sed_rag.ingestion.cli extract-text "../statut_puet (2).pdf" -o corpus/ocr/statut.ocr.txt
    python -m sed_rag.ingestion.cli ingest-manifest corpus/manifest.toml [--only "Статут ПУЕТ"] [--force]
    python -m sed_rag.ingestion.cli ingest-file corpus/samples/x.txt --title "…" --level ministry --issuer "МОН України"
    python -m sed_rag.ingestion.cli search "строк контракту ректора" [--as-of 2026-10-17]
    python -m sed_rag.ingestion.cli reconcile            # звірка Qdrant ↔ regulatory_chunks

Налаштування — змінні середовища `SED_RAG_*` (див. `sed_rag/config.py`). `--no-db` індексує лише
в Qdrant (для локальних експериментів; перевірки з такого індексу не можна зберегти в БД).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient, models
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sed_db.enums import RegulatoryLevel, RegulatoryStatus

from ..adapters.embeddings import OpenAICompatibleEmbedder, TeiReranker
from ..adapters.memory import MemoryCorpusRepository
from ..adapters.qdrant_index import QdrantVectorIndex
from ..adapters.sql_corpus import SqlCorpusRepository
from ..config import Settings
from ..corpus.sparse import Bm25Encoder
from ..domain.models import ActMeta
from ..retrieval.hybrid import HybridRetriever
from .service import CorpusIngestor
from .text_sources import read_act_file

SYSTEM_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")   # sed_db.seeds.SYSTEM_USER_ID


def _date(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def act_from_dict(d: dict[str, Any]) -> ActMeta:
    return ActMeta(
        title=d["title"], short_title=d.get("short_title"), level=RegulatoryLevel(d["level"]), issuer=d["issuer"],
        status=RegulatoryStatus(d.get("status", "in_force")), doc_number=d.get("doc_number"),
        adopted_on=_date(d.get("adopted_on")), effective_from=_date(d.get("effective_from")),
        effective_to=_date(d.get("effective_to")), source_url=d.get("source_url"),
        is_sample=bool(d.get("is_sample", False)),
    )


class _Ctx:
    def __init__(self, settings: Settings, no_db: bool, qdrant_path: str | None) -> None:
        self.s = settings
        self.no_db = no_db
        self.qdrant = (AsyncQdrantClient(path=qdrant_path) if qdrant_path else
                       AsyncQdrantClient(url=settings.qdrant_url, timeout=settings.qdrant_timeout_s,
                                         api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None))
        self.http = httpx.AsyncClient(base_url=settings.embedding_base_url, timeout=httpx.Timeout(120.0, connect=5.0),
                                      headers={"Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"})
        self.engine = None if no_db else create_async_engine(settings.database_url, pool_pre_ping=True)
        self.index = QdrantVectorIndex(self.qdrant, settings.qdrant_collection)
        self.embedder = OpenAICompatibleEmbedder(
            self.http, settings.embedding_model, settings.embedding_model_version, settings.embedding_dim,
            query_prefix=settings.embedding_query_prefix, passage_prefix=settings.embedding_passage_prefix,
            batch_size=settings.embedding_batch_size)
        repo = (MemoryCorpusRepository() if self.engine is None
                else SqlCorpusRepository(async_sessionmaker(self.engine, expire_on_commit=False)))
        self.ingestor = CorpusIngestor(repository=repo, index=self.index, embedder=self.embedder, bm25=Bm25Encoder())

    async def close(self) -> None:
        await self.http.aclose()
        await self.qdrant.close()
        if self.engine is not None:
            await self.engine.dispose()


async def _ingest_one(ctx: _Ctx, act: ActMeta, path: Path, quality: float | None, force: bool,
                      force_ocr: bool = False) -> None:
    extracted = read_act_file(path, force_ocr=force_ocr)
    q = quality if quality is not None else extracted.quality
    rep = await ctx.ingestor.ingest(act, extracted.text, actor_user_id=SYSTEM_USER_ID, text_quality=q, force=force)
    print(f"{rep.status.value:10} {act.label}: ревізія {rep.index_revision}, фрагментів {rep.chunks}"
          f"{f', OCR-сторінок {extracted.ocr_pages}, якість {q:.2f}' if extracted.ocr_pages else ''}")


async def cmd_ingest_manifest(args: argparse.Namespace, ctx: _Ctx) -> int:
    manifest = Path(args.manifest)
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    errors = 0
    for item in data.get("act", []):
        act = act_from_dict(item)
        if args.only and args.only not in (act.label, act.title):
            continue
        path = (manifest.parent / item["file"]).resolve()
        if not path.exists():
            print(f"ПРОПУЩЕНО  {act.label}: файл {path} не знайдено", file=sys.stderr)
            errors += 1
            continue
        await _ingest_one(ctx, act, path, item.get("text_quality"), args.force, bool(item.get("force_ocr")))
    return 1 if errors else 0


async def cmd_ingest_file(args: argparse.Namespace, ctx: _Ctx) -> int:
    act = act_from_dict({k: getattr(args, k) for k in (
        "title", "short_title", "level", "issuer", "status", "doc_number", "adopted_on", "effective_from",
        "effective_to", "source_url", "is_sample")})
    await _ingest_one(ctx, act, Path(args.file), args.text_quality, args.force)
    return 0


async def cmd_search(args: argparse.Namespace, ctx: _Ctx) -> int:
    reranker = None
    if ctx.s.reranker_url:
        reranker = TeiReranker(httpx.AsyncClient(base_url=ctx.s.reranker_url, timeout=30.0))
    r = HybridRetriever(ctx.index, ctx.embedder, Bm25Encoder(), ctx.s.retrieval, reranker)
    res = await r.retrieve(args.query, _date(args.as_of) or date.today(), k=args.k)
    print(f"покриття: {res.coverage.value}")
    for c in res.clauses:
        print(f"\n[{c.fused_score:.3f} d={c.dense_score} s={c.sparse_score}] {c.citation_label}\n{c.content[:400]}")
    return 0


async def cmd_reconcile(args: argparse.Namespace, ctx: _Ctx) -> int:
    """Точки Qdrant без рядка в БД видаляються; актуальні фрагменти БД без точки — звіт (потрібен --force)."""
    if ctx.engine is None:
        print("reconcile потребує БД", file=sys.stderr)
        return 2
    from sed_db.models import RegulatoryChunk

    async with async_sessionmaker(ctx.engine)() as s:
        db_ids = {str(i) for i in (await s.scalars(select(RegulatoryChunk.id))).all()}
        db_current = {str(i) for i in (await s.scalars(
            select(RegulatoryChunk.id).where(RegulatoryChunk.is_current.is_(True)))).all()}
    q_ids: set[str] = set()
    q_current: set[str] = set()
    offset: Any = None
    while True:
        pts, offset = await ctx.qdrant.scroll(ctx.s.qdrant_collection, limit=1024, offset=offset,
                                              with_payload=["is_current"], with_vectors=False)
        for p in pts:
            q_ids.add(str(p.id))
            if (p.payload or {}).get("is_current"):
                q_current.add(str(p.id))
        if offset is None:
            break
    orphans = q_ids - db_ids
    stale = q_current - db_current - orphans
    missing = db_current - q_ids
    if orphans:
        await ctx.index.delete_points([uuid.UUID(i) for i in orphans])
    if stale:
        await ctx.qdrant.set_payload(ctx.s.qdrant_collection, payload={"is_current": False},
                                     points=models.PointIdsList(points=list(stale)), wait=True)
    print(json.dumps({"deleted_orphans": len(orphans), "retired_stale": len(stale),
                      "missing_in_qdrant": len(missing)}, ensure_ascii=False))
    return 1 if missing else 0


def cmd_extract_text(args: argparse.Namespace) -> int:
    r = read_act_file(Path(args.file), force_ocr=args.force_ocr)
    out = Path(args.output) if args.output else None
    if out:
        out.write_text(r.text, encoding="utf-8")
    else:
        sys.stdout.write(r.text)
    print(f"сторінок {r.pages}, з них OCR {r.ocr_pages}, якість {r.quality:.2f}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sed_rag.ingestion.cli", description="Нормативний корпус СЕД ПУЕТ")
    p.add_argument("--no-db", action="store_true", help="лише Qdrant, без PostgreSQL")
    p.add_argument("--qdrant-path", help="локальний Qdrant у каталозі (без сервера)")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract-text", help="PDF/скан → текст для вичитки")
    e.add_argument("file")
    e.add_argument("-o", "--output")
    e.add_argument("--force-ocr", action="store_true", help="ігнорувати текстовий шар PDF")

    m = sub.add_parser("ingest-manifest", help="проіндексувати акти з manifest.toml")
    m.add_argument("manifest")
    m.add_argument("--only")
    m.add_argument("--force", action="store_true")

    f = sub.add_parser("ingest-file", help="проіндексувати один акт")
    f.add_argument("file")
    f.add_argument("--title", required=True)
    f.add_argument("--short-title")
    f.add_argument("--level", required=True, choices=[x.value for x in RegulatoryLevel])
    f.add_argument("--issuer", required=True)
    f.add_argument("--status", default="in_force", choices=[x.value for x in RegulatoryStatus])
    f.add_argument("--doc-number")
    f.add_argument("--adopted-on")
    f.add_argument("--effective-from")
    f.add_argument("--effective-to")
    f.add_argument("--source-url")
    f.add_argument("--is-sample", action="store_true")
    f.add_argument("--text-quality", type=float)
    f.add_argument("--force", action="store_true")

    s = sub.add_parser("search", help="перевірити пошук")
    s.add_argument("query")
    s.add_argument("--as-of")
    s.add_argument("-k", type=int, default=8)

    sub.add_parser("reconcile", help="звірити Qdrant з regulatory_chunks")

    args = p.parse_args(argv)
    if args.cmd == "extract-text":
        return cmd_extract_text(args)

    async def run() -> int:
        ctx = _Ctx(Settings(), args.no_db, args.qdrant_path)
        try:
            handler = {"ingest-manifest": cmd_ingest_manifest, "ingest-file": cmd_ingest_file,
                       "search": cmd_search, "reconcile": cmd_reconcile}[args.cmd]
            return await handler(args, ctx)
        finally:
            await ctx.close()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
