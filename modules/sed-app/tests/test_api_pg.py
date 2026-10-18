"""Наскрізні сценарії на живому PostgreSQL: вхід за ролями, реєстрація документа, картка,
ручна валідація ШІ, видимість документів, маршрут погодження з КЕП, журнал аудиту."""

from __future__ import annotations

import base64
import uuid
from typing import Any

from sqlalchemy import select, text

from sed_db.enums import AccessLevel, StepStatus
from sed_db.models import AuditLog, Document

from .conftest import ORDER_LINES, Stack, token, upload


# --- автентифікація ---------------------------------------------------------------------------

async def test_auth_config_and_dev_users(client: Any, stack: Stack) -> None:
    r = await client.get("/api/v1/auth/config")
    assert r.json()["mode"] == "dev"
    users = (await client.get("/api/v1/auth/dev-users")).json()
    assert len(users) == 10
    roles = {role for u in users for role in u["roles"]}
    assert len(roles) == 9, roles   # усі 9 ролей СЕД представлені


async def test_dev_login_and_me(client: Any, stack: Stack) -> None:
    uid = stack.users["head.kknit"]["id"]
    r = await client.post("/api/v1/auth/dev-login", json={"user_id": str(uid)})
    assert r.status_code == 200
    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    body = me.json()
    assert body["display_name"] == "Наталія КРАВЕЦЬ"
    assert {x["role"] for x in body["roles"]} == {"department_head", "lecturer"}
    assert body["permissions"]["view_all_documents"] is False


async def test_no_or_bad_token_is_401(client: Any, stack: Stack) -> None:
    assert (await client.get("/api/v1/me")).status_code == 401
    bad = token(stack, "rector")["Authorization"][:-4] + "abcd"
    assert (await client.get("/api/v1/me", headers={"Authorization": bad})).status_code == 401


async def test_role_revoked_in_token_is_not_effective(client: Any, stack: Stack) -> None:
    """Роль є в БД, але Keycloak її вже відкликав (немає в токені) — СЕД її не враховує."""
    me = (await client.get("/api/v1/me", headers=token(stack, "rector", roles=[]))).json()
    assert me["roles"] == [] and me["permissions"]["view_all_documents"] is False


async def test_unknown_user_is_provisioned_without_roles(client: Any, stack: Stack) -> None:
    tok, _ = stack.issuer.issue(sub=uuid.uuid4(), username="new", email=f"new-{uuid.uuid4().hex[:6]}@puet.edu.ua",
                                given_name="Нова", family_name="Людина", roles=["rector"])
    me = (await client.get("/api/v1/me", headers={"Authorization": f"Bearer {tok}"})).json()
    assert me["display_name"] == "Нова ЛЮДИНА" and me["roles"] == []   # роль лише в токені — не діє


# --- реєстрація, картка, ручна валідація --------------------------------------------------------

async def test_upload_and_card(client: Any, stack: Stack) -> None:
    res = await upload(client, stack, "lecturer")
    assert res["document_type_code"] == "MEMO"
    card = (await client.get(f"/api/v1/documents/{res['document_id']}", headers=token(stack, "lecturer"))).json()
    assert card["title"].startswith("Про закупівлю")
    assert card["document_type"]["code"] == "MEMO" and card["status"] == "draft"
    keys = [f["key"] for f in card["extraction"]["fields"]]
    assert keys[:2] == ["document_type", "document_date"]
    assert card["permissions"]["review"] and card["permissions"]["start_route"]
    assert card["versions"][0]["has_text"]


async def test_low_confidence_field_review(client: Any, stack: Stack) -> None:
    stack.llm.low_confidence = True
    try:
        res = await upload(client, stack, "lecturer")
    finally:
        stack.llm.low_confidence = False
    assert res["requires_human_review"]
    doc_id = res["document_id"]
    h = token(stack, "head.kknit")
    card = (await client.get(f"/api/v1/documents/{doc_id}", headers=h)).json()
    pending = [f for f in card["extraction"]["fields"] if f["review_status"] == "pending"]
    assert pending and all(float(f["confidence"]) < 0.85 for f in pending)

    queue = (await client.get("/api/v1/review-queue", headers=h)).json()
    assert any(q["document_id"] == doc_id for q in queue)
    student_queue = (await client.get("/api/v1/review-queue", headers=token(stack, "student"))).json()
    assert all(q["document_id"] != doc_id for q in student_queue)

    author_field = next(f for f in pending if f["key"] == "author")
    stale = await client.post(f"/api/v1/documents/{doc_id}/fields/{author_field['id']}/review", headers=h,
                              json={"decision": "correct", "value": {"full_name": "Наталія Кравець",
                                                                     "position": "Завідувач кафедри"},
                                    "row_version": card["row_version"] + 5})
    assert stale.status_code == 409
    r = await client.post(f"/api/v1/documents/{doc_id}/fields/{author_field['id']}/review", headers=h,
                          json={"decision": "correct", "value": {"full_name": "Наталія Кравець",
                                                                 "position": "Завідувач кафедри"},
                                "row_version": card["row_version"]})
    assert r.status_code == 204, r.text
    # Решту реквізитів, що чекали перевірки, підтверджуємо
    for f in card["extraction"]["fields"]:
        if f["review_status"] == "pending" and f["id"] != author_field["id"]:
            assert (await client.post(f"/api/v1/documents/{doc_id}/fields/{f['id']}/review", headers=h,
                                      json={"decision": "confirm"})).status_code == 204
    card = (await client.get(f"/api/v1/documents/{doc_id}", headers=h)).json()
    author = next(f for f in card["extraction"]["fields"] if f["key"] == "author")
    assert author["review_status"] == "corrected" and author["reviewed_by"]["name"] == "Наталія КРАВЕЦЬ"
    assert card["requisites"]["author"]["full_name"] == "Наталія Кравець"
    assert card["extraction"]["run"]["review_status"] == "corrected"   # усі реквізити розглянуто
    queue = (await client.get("/api/v1/review-queue", headers=h)).json()
    assert all(q["document_id"] != doc_id for q in queue)


async def test_reject_requires_comment_and_student_cannot_review(client: Any, stack: Stack) -> None:
    res = await upload(client, stack, "lecturer")
    card = (await client.get(f"/api/v1/documents/{res['document_id']}", headers=token(stack, "lecturer"))).json()
    fid = card["extraction"]["fields"][0]["id"]
    url = f"/api/v1/documents/{res['document_id']}/fields/{fid}/review"
    assert (await client.post(url, headers=token(stack, "lecturer"), json={"decision": "reject"})).status_code == 422
    # студент кафедри не бачить внутрішній документ викладача → 404, а не 403
    assert (await client.post(url, headers=token(stack, "student"), json={"decision": "confirm"})).status_code == 404


async def test_manual_entry_when_llm_down(client: Any, stack: Stack) -> None:
    stack.llm.down = True
    try:
        res = await upload(client, stack, "clerk")
    finally:
        stack.llm.down = False
    assert res["status"] == "manual_entry_required" and res["requisites"] is None
    doc_id, h = res["document_id"], token(stack, "clerk")
    bad = await client.put(f"/api/v1/documents/{doc_id}/requisites", headers=h,
                           json={"values": {"document_date": "17/10/2026"}})
    assert bad.status_code == 422 and "РРРР-ММ-ДД" in bad.json()["detail"]
    r = await client.put(f"/api/v1/documents/{doc_id}/requisites", headers=h, json={"values": {
        "document_date": "2026-10-17", "title": "Про графік відпусток", "author": "Оксана Павленко"}})
    assert r.status_code == 204, r.text
    card = (await client.get(f"/api/v1/documents/{doc_id}", headers=h)).json()
    assert card["title"] == "Про графік відпусток"
    assert card["requisites"]["author"] == {"full_name": "Оксана Павленко", "position": None, "org_unit": None}
    assert card["extraction"]["run"]["review_status"] == "corrected"


# --- видимість ---------------------------------------------------------------------------------

async def test_visibility_matrix_matches_policy(client: Any, stack: Stack, sessionmaker: Any) -> None:
    """SQL-фільтр списку і `can_view` (картка) дають однаковий результат для всіх 10 користувачів."""
    internal = await upload(client, stack, "lecturer")
    restricted = await upload(client, stack, "lecturer", access_level="personal_data")
    public = await upload(client, stack, "clerk", access_level="public")
    docs = {"internal": internal["document_id"], "restricted": restricted["document_id"],
            "public": public["document_id"]}
    expected = {
        "rector": {"internal", "restricted", "public"}, "vicerector": {"internal", "restricted", "public"},
        "director": {"internal", "restricted", "public"},   # кафедра входить до його інституту
        "head.kknit": {"internal", "restricted", "public"}, "lecturer": {"internal", "restricted", "public"},
        "student": {"public"}, "studgov": {"public"}, "buh": {"public"}, "pfv": {"public"},
        "clerk": {"public"},
    }
    for login, should in expected.items():
        h = token(stack, login)
        listed = {i["id"] for i in (await client.get("/api/v1/documents?limit=100", headers=h)).json()["items"]}
        for name, doc_id in docs.items():
            card = await client.get(f"/api/v1/documents/{doc_id}", headers=h)
            assert (card.status_code == 200) == (name in should), (login, name, card.status_code)
            assert (doc_id in listed) == (name in should), (login, name)


async def test_list_filters_and_search(client: Any, stack: Stack) -> None:
    res = await upload(client, stack, "lecturer", lines=[*ORDER_LINES[:3], "Про ремонт аудиторії 214", "Текст."])
    h = token(stack, "lecturer")
    found = (await client.get("/api/v1/documents", params={"q": "аудиторії"}, headers=h)).json()
    assert [i["id"] for i in found["items"]] == [res["document_id"]]
    mine = (await client.get("/api/v1/documents", params={"mine": "true", "type_code": "MEMO"}, headers=h)).json()
    assert mine["total"] >= 1 and all(i["document_type_code"] == "MEMO" for i in mine["items"])
    weird = (await client.get("/api/v1/documents", params={"q": "100%_\\"}, headers=h))
    assert weird.status_code == 200


# --- файл і журнал аудиту ------------------------------------------------------------------------

async def test_download_is_audited(client: Any, stack: Stack, sessionmaker: Any) -> None:
    res = await upload(client, stack, "lecturer")
    doc_id, ver_id = res["document_id"], res["version_id"]
    r = await client.get(f"/api/v1/documents/{doc_id}/versions/{ver_id}/file", headers=token(stack, "head.kknit"))
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["content-type"] == "application/pdf"
    denied = await client.get(f"/api/v1/documents/{doc_id}/versions/{ver_id}/file", headers=token(stack, "buh"))
    assert denied.status_code == 404
    async with sessionmaker() as s:
        actions = (await s.scalars(select(AuditLog.action).where(AuditLog.document_id == uuid.UUID(doc_id),
                                                                 AuditLog.category == "business_event"))).all()
        assert "file.download" in actions
        assert (await s.execute(text("SELECT * FROM sed_audit_verify()"))).all() == []   # хеш-ланцюжок цілий


# --- нормативний аудит ---------------------------------------------------------------------------

async def test_compliance_uses_current_version_and_policy(client: Any, stack: Stack) -> None:
    res = await upload(client, stack, "lecturer")
    r = await client.post(f"/api/v1/documents/{res['document_id']}/compliance-checks",
                          headers=token(stack, "head.kknit"))
    assert r.status_code == 201, r.text
    assert stack.compliance.calls[-1] == (stack.users["head.kknit"]["sub"], uuid.UUID(res["version_id"]))
    denied = await client.post(f"/api/v1/documents/{res['document_id']}/compliance-checks",
                               headers=token(stack, "student"))
    assert denied.status_code == 404
    # Ендпоінт модуля з ширшою політикою доступу не опубліковано
    hidden = await client.post(f"/api/v1/documents/versions/{res['version_id']}/compliance-checks",
                               headers=token(stack, "student"))
    assert hidden.status_code in (404, 405)


# --- маршрут погодження з КЕП -----------------------------------------------------------------------

async def _sign(client: Any, stack: Stack, login: str, step_id: str) -> dict[str, Any]:
    h = token(stack, login)
    mock = await client.post(f"/api/v1/dev/steps/{step_id}/mock-signature", headers=h)
    assert mock.status_code == 200, mock.text
    r = await client.post(f"/api/v1/steps/{step_id}/sign", headers=h,
                          json={"signature_base64": mock.json()["signature_base64"], "provider": "file_key"})
    assert r.status_code == 200, r.text
    return r.json()


async def test_memo_procurement_route_end_to_end(client: Any, stack: Stack, sessionmaker: Any) -> None:
    res = await upload(client, stack, "lecturer")
    doc_id = res["document_id"]
    author = token(stack, "lecturer")
    preview = await client.post(f"/api/v1/documents/{doc_id}/route-preview", headers=author, json={})
    assert preview.status_code == 200, preview.text
    chain = [s["assignee_name"] for s in preview.json()["steps"]]
    assert chain == ["Наталія КРАВЕЦЬ", "Ірина САВЧУК", "Людмила РОМАНЕНКО", "Олександр ДЕМЧЕНКО", "Тетяна ЛИТВИН"]

    r = await client.post(f"/api/v1/documents/{doc_id}/routes", headers=author, json={})
    assert r.status_code == 201, r.text

    order = ["head.kknit", "vicerector", "buh", "rector", "pfv"]
    for i, login in enumerate(order):
        inbox = (await client.get("/api/v1/approvals/inbox", headers=token(stack, login))).json()
        mine = [x for x in inbox if x["document_id"] == doc_id]
        assert len(mine) == 1, (login, inbox)
        # наступні учасники ще не бачать кроку в черзі
        for later in order[i + 1:]:
            later_inbox = (await client.get("/api/v1/approvals/inbox", headers=token(stack, later))).json()
            assert all(x["document_id"] != doc_id for x in later_inbox)
        card = (await client.get(f"/api/v1/documents/{doc_id}", headers=token(stack, login))).json()
        assert card["permissions"]["my_active_step_id"] == mine[0]["step_id"]
        await _sign(client, stack, login, mine[0]["step_id"])

    card = (await client.get(f"/api/v1/documents/{doc_id}", headers=author)).json()
    assert card["route"]["status"] == "completed"
    assert [s["status"] for s in card["route"]["steps"]] == [StepStatus.APPROVED.value] * 5
    assert len(card["signatures"]) == 5 and all(s["validation_status"] == "valid" for s in card["signatures"])
    assert card["permissions"]["start_route"] is False
    # Учасник маршруту з іншого підрозділу бачить документ, хоча гриф «внутрішній»
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=token(stack, "pfv"))).status_code == 200


async def test_reject_returns_route(client: Any, stack: Stack) -> None:
    res = await upload(client, stack, "lecturer")
    doc_id = res["document_id"]
    assert (await client.post(f"/api/v1/documents/{doc_id}/routes", headers=token(stack, "lecturer"),
                              json={})).status_code == 201
    step = (await client.get("/api/v1/approvals/inbox", headers=token(stack, "head.kknit"))).json()
    step_id = next(x["step_id"] for x in step if x["document_id"] == doc_id)
    no_reason = await client.post(f"/api/v1/steps/{step_id}/decision", headers=token(stack, "head.kknit"),
                                  json={"decision": "rejected"})
    assert no_reason.status_code == 422
    r = await client.post(f"/api/v1/steps/{step_id}/decision", headers=token(stack, "head.kknit"),
                          json={"decision": "rejected", "comment": "Додайте комерційні пропозиції"})
    assert r.status_code == 200, r.text
    card = (await client.get(f"/api/v1/documents/{doc_id}", headers=token(stack, "lecturer"))).json()
    assert card["route"]["status"] == "rejected" and card["status"] == "revision_required"
    assert card["permissions"]["start_route"] is True


async def test_signature_of_another_person_is_rejected(client: Any, stack: Stack) -> None:
    res = await upload(client, stack, "lecturer")
    doc_id = res["document_id"]
    await client.post(f"/api/v1/documents/{doc_id}/routes", headers=token(stack, "lecturer"), json={})
    step_id = next(x["step_id"] for x in (await client.get("/api/v1/approvals/inbox",
                                                            headers=token(stack, "head.kknit"))).json()
                   if x["document_id"] == doc_id)
    # викладач намагається завізувати крок завідувача
    r = await client.post(f"/api/v1/steps/{step_id}/sign", headers=token(stack, "lecturer"),
                          json={"signature_base64": base64.b64encode(b"x" * 64).decode()})
    assert r.status_code in (403, 422)


async def test_access_level_enum_roundtrip(sessionmaker: Any, client: Any, stack: Stack) -> None:
    res = await upload(client, stack, "lecturer", access_level="restricted")
    async with sessionmaker() as s:
        doc = await s.get(Document, uuid.UUID(res["document_id"]))
        assert doc is not None and doc.access_level is AccessLevel.RESTRICTED


async def test_accept_confident_moves_values_to_card(client: Any, stack: Stack) -> None:
    res = await upload(client, stack, "lecturer")
    doc_id = res["document_id"]
    h = token(stack, "lecturer")
    card = (await client.get(f"/api/v1/documents/{doc_id}", headers=h)).json()
    confident = [f for f in card["extraction"]["fields"] if f["review_status"] == "not_required"]
    assert confident and not card["requisites"]   # без дії людини картка порожня
    assert (await client.post(f"/api/v1/documents/{doc_id}/requisites/accept-confident",
                              headers=token(stack, "student"), json={})).status_code in (403, 404)
    r = await client.post(f"/api/v1/documents/{doc_id}/requisites/accept-confident", headers=h,
                          json={"row_version": card["row_version"]})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] >= 1
    card = (await client.get(f"/api/v1/documents/{doc_id}", headers=h)).json()
    assert card["requisites"]
    left = [f for f in card["extraction"]["fields"] if f["review_status"] == "not_required"]
    assert len(left) == len(confident) - r.json()["accepted"]   # порожні/некоректні лишаються людині
    again = await client.post(f"/api/v1/documents/{doc_id}/requisites/accept-confident", headers=h, json={})
    assert again.status_code == 422


async def test_web_shell_and_security_headers(client: Any, stack: Stack) -> None:
    r = await client.get("/")
    assert r.status_code == 200 and "<html" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert (await client.head("/")).status_code == 200
    js = await client.get("/static/js/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    api = await client.get("/api/v1/auth/config")
    assert api.headers["cache-control"] == "no-store"
