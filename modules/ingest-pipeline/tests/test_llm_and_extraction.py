"""LLM-адаптер (httpx.MockTransport замість vLLM/Ollama) і сервіс вилучення зі скорингом."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from sed_ingest.adapters.ocr_engine import TesseractOcrEngine
from sed_ingest.adapters.openai_compat_llm import OpenAICompatibleLlm
from sed_ingest.domain.llm_schema import llm_json_schema
from sed_ingest.domain.models import FileKind, IncomingFile, LlmError, LlmIdentity, OcrResult, PageText, TextSource
from sed_ingest.domain.requisites import DocumentKind, PartyRole
from sed_ingest.pipeline.extraction import RequisitesExtractor
from sed_ingest.pipeline.pii import RegexPseudonymizer

from .conftest import ORDER_TEXT, ScriptedLlm, document_block, order_responder

TODAY = date(2026, 10, 17)
IDENTITY = LlmIdentity("mistral", "1", "vllm")


def _ocr(text: str, quality: float = 1.0) -> OcrResult:
    source = TextSource.TEXT_LAYER if quality == 1.0 else TextSource.OCR
    return OcrResult(pages=(PageText(1, text, source, quality),), engine="t", engine_version="5", page_count=1)


def _chat_response(content: str, finish: str = "stop") -> dict[str, Any]:
    return {"model": "mistral", "choices": [{"message": {"role": "assistant", "content": content},
                                             "finish_reason": finish}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 150}}


def _llm(handler) -> OpenAICompatibleLlm:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(base_url="http://llm/v1", transport=httpx.MockTransport(handler))
    return OpenAICompatibleLlm(client, "mistral", IDENTITY, backoff_s=0)


# --- адаптер --------------------------------------------------------------------------


async def test_adapter_requests_strict_json_schema_and_parses_answer() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        assert req.url.path == "/v1/chat/completions"
        return httpx.Response(200, json=_chat_response('```json\n{"ok": true}\n```'))

    out = await _llm(handler).complete_json([{"role": "user", "content": "x"}], llm_json_schema(), "s")
    rf = seen["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["properties"].keys() >= {"document_type", "interested_parties"}
    assert seen["temperature"] == 0 and seen["seed"] == 42
    assert out.content == {"ok": True} and out.tokens_in == 900 and out.tokens_out == 150


async def test_adapter_retries_transient_failures() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused")
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=_chat_response("{}"))

    await _llm(handler).complete_json([], {}, "s")
    assert calls["n"] == 3


@pytest.mark.parametrize("response, message", [
    (httpx.Response(400, json={"error": "response_format not supported"}), "відхилив"),
    (httpx.Response(200, json=_chat_response('{"a": 1', finish="length")), "обрізано"),
    (httpx.Response(200, json=_chat_response("це не JSON")), "невалідний JSON"),
    (httpx.Response(200, json=_chat_response("[1, 2]")), "не є об'єктом"),
    (httpx.Response(200, json={"unexpected": True}), "неочікуваному"),
    (httpx.Response(503), "недоступна"),
])
async def test_adapter_errors(response: httpx.Response, message: str) -> None:
    with pytest.raises(LlmError, match=message):
        await _llm(lambda _: response).complete_json([], {}, "s")


# --- вилучення: щасливий шлях і HITL --------------------------------------------------


async def test_grounded_extraction_passes_threshold_and_restores_pii() -> None:
    llm = ScriptedLlm(order_responder)
    out = await RequisitesExtractor(llm, RegexPseudonymizer(), 24_000).extract(_ocr(ORDER_TEXT), TODAY)
    req = out.requisites
    assert req is not None and out.error is None
    assert req.document_type.value is DocumentKind.ORDER_MAIN
    assert req.document_date.value == date(2026, 10, 17)
    assert req.registration_number.value == "245-од"
    assert req.title.value == "Про призначення відповідальних за організацію практики"
    assert req.author.value is not None and req.author.value.full_name == "Іван РЕКТОРЕНКО"
    parties = req.interested_parties.value or []
    assert [(p.full_name, p.role) for p in parties] == [
        ("Петренко Олену Іванівну", PartyRole.EXECUTOR),
        ("Коваленка Андрія Петровича", PartyRole.CONTROLLER),
    ]
    assert req.confidence >= Decimal("0.85") and not req.requires_human_review
    assert all(f.confidence <= Decimal("0.97") for f in req.fields().values())

    # Модель не бачила жодного ПІБ, а в ai_runs.output лишаються тільки мітки
    sent = json.dumps(llm.calls, ensure_ascii=False)
    for name in ("Петренко", "Коваленка", "РЕКТОРЕНКО"):
        assert name not in sent and name not in json.dumps(out.masked_output, ensure_ascii=False)
    assert out.pii_entities_masked == 3 and out.pii_counts == {"ОСОБА": 3}
    expected_hash = hashlib.sha256((llm.calls[0][0]["content"] + "\n" + llm.calls[0][1]["content"]).encode())
    assert out.input_sha256 == expected_hash.hexdigest()


async def test_hallucinated_value_is_sent_to_review() -> None:
    def liar(messages: list[dict[str, str]]) -> dict[str, Any]:
        r = order_responder(messages)
        r["registration_number"] = {"value": "999-к", "evidence": "№ 999-к від 01.09.2026", "confidence": 0.99}
        r["author"]["value"]["full_name"] = "Марія Вигадана"
        return r

    req = (await RequisitesExtractor(ScriptedLlm(liar), RegexPseudonymizer(), 24_000)
           .extract(_ocr(ORDER_TEXT), TODAY)).requisites
    assert req is not None and req.requires_human_review
    assert req.registration_number.confidence < Decimal("0.5")
    assert any("галюцинація" in i for i in req.registration_number.issues)
    assert req.author.confidence < Decimal("0.85")
    assert req.document_date.confidence >= Decimal("0.85")      # решта реквізитів не постраждала


async def test_missing_required_requisite_scores_zero() -> None:
    def no_number(messages: list[dict[str, str]]) -> dict[str, Any]:
        r = order_responder(messages)
        r["registration_number"] = {"value": None, "evidence": None, "confidence": 0.9}
        return r

    req = (await RequisitesExtractor(ScriptedLlm(no_number), RegexPseudonymizer(), 24_000)
           .extract(_ocr(ORDER_TEXT), TODAY)).requisites
    assert req is not None
    assert req.registration_number.confidence == 0 and req.confidence == 0 and req.requires_human_review


async def test_number_not_required_for_application() -> None:
    text = "Ректору ПУЕТ\nЗАЯВА\nПрошу надати академічну відпустку.\n17.10.2026 Олена ПЕТРЕНКО"

    def application(messages: list[dict[str, str]]) -> dict[str, Any]:
        doc = document_block(messages)
        sign = [ln for ln in doc.splitlines() if "17.10.2026" in ln][0]
        token = sign.split()[-1]
        return {
            "document_type": {"value": "application", "evidence": "ЗАЯВА", "confidence": 0.95},
            "document_date": {"value": "17.10.2026", "evidence": sign, "confidence": 0.95},
            "registration_number": {"value": None, "evidence": None, "confidence": 0.95},
            "author": {"value": {"full_name": token, "position": None, "org_unit": None, "evidence": sign},
                       "confidence": 0.95},
            "title": {"value": None, "evidence": None, "confidence": 0.95},
            "summary": {"value": "Прохання надати академічну відпустку.", "confidence": 0.95},
            "interested_parties": {"value": [{"full_name": None, "position": "Ректор", "org_unit": "ПУЕТ",
                                              "evidence": "Ректору ПУЕТ", "role": "addressee"}],
                                   "confidence": 0.95},
        }

    req = (await RequisitesExtractor(ScriptedLlm(application), RegexPseudonymizer(), 24_000)
           .extract(_ocr(text), TODAY)).requisites
    assert req is not None and not req.requires_human_review, req.model_dump()
    assert req.registration_number.value is None and req.author.value.full_name == "Олена ПЕТРЕНКО"  # type: ignore[union-attr]


async def test_future_date_and_wrong_kind_are_flagged() -> None:
    def wrong(messages: list[dict[str, str]]) -> dict[str, Any]:
        r = order_responder(messages)
        r["document_type"] = {"value": "protocol", "evidence": "НАКАЗ", "confidence": 0.95}
        return r

    text = ORDER_TEXT.replace("17.10.2026", "17.10.2027")
    req = (await RequisitesExtractor(ScriptedLlm(wrong), RegexPseudonymizer(), 24_000)
           .extract(_ocr(text), TODAY)).requisites
    assert req is not None
    assert req.document_date.value is None and req.document_date.confidence <= Decimal("0.3")
    assert req.document_type.confidence < Decimal("0.85")


async def test_invalid_schema_is_repaired_once() -> None:
    def broken(messages: list[dict[str, str]]) -> dict[str, Any]:
        r = copy.deepcopy(order_responder(messages))
        r["document_type"]["value"] = "наказ"          # не з переліку
        del r["summary"]
        return r

    llm = ScriptedLlm(broken, order_responder)
    out = await RequisitesExtractor(llm, RegexPseudonymizer(), 24_000).extract(_ocr(ORDER_TEXT), TODAY)
    assert out.requisites is not None and out.completion is not None
    assert len(llm.calls) == 2 and out.completion.attempts == 2 and out.completion.tokens_in == 2000
    assert "не відповідає JSON-схемі" in llm.calls[1][-1]["content"]


async def test_llm_down_degrades_to_manual_entry() -> None:
    out = await RequisitesExtractor(ScriptedLlm(LlmError("Локальна LLM недоступна")), RegexPseudonymizer(),
                                    24_000).extract(_ocr(ORDER_TEXT), TODAY)
    assert out.requisites is None and out.error == "Локальна LLM недоступна"


async def test_empty_ocr_skips_llm() -> None:
    llm = ScriptedLlm(order_responder)
    out = await RequisitesExtractor(llm, RegexPseudonymizer(), 24_000).extract(_ocr("   ", 0.0), TODAY)
    assert out.requisites is None and not llm.calls and "вручну" in (out.error or "")


async def test_ocr_misread_lowers_only_affected_field(ocr_engine: TesseractOcrEngine, order_png: bytes) -> None:
    """Скан: «№ 245-од» розпізнано з помилкою — номер іде на ручну валідацію, решта — ні."""
    ocr = ocr_engine.extract(IncomingFile(order_png, "s.png", FileKind.PNG, hashlib.sha256(order_png).hexdigest()))
    req = (await RequisitesExtractor(ScriptedLlm(order_responder), RegexPseudonymizer(), 24_000)
           .extract(ocr, TODAY)).requisites
    assert req is not None
    assert req.document_date.value == date(2026, 10, 17) and not req.document_date.requires_human_review
    assert req.title.confidence >= Decimal("0.85")
    if req.registration_number.value != "245-од":
        assert req.registration_number.requires_human_review
        assert any("якість розпізнавання" in i for i in req.registration_number.issues)
        assert req.requires_human_review
