"""HTTP API поверх живої БД: увесь ланцюжок погодження через ендпоінти, включно з імітацією КЕП."""

from __future__ import annotations

import asyncio
import base64
import uuid

import httpx
import pytest

from sed_routing.api.app import MockSigningDesk, create_app
from sed_routing.config import Settings
from sed_routing.service import Caller

from .conftest import make_memo


class FakeVerifier:
    """Токен = keycloak sub; суфікс «+ai» додає роль сервісного акаунта ШІ."""

    def verify(self, token: str) -> Caller:
        from sed_routing.api.auth import InvalidToken

        sub, _, flag = token.partition("+")
        try:
            return Caller(uuid.UUID(sub), frozenset({"sed-ai-service"} if flag == "ai" else set()))
        except ValueError as exc:
            raise InvalidToken(str(exc)) from exc


@pytest.fixture()
async def client(service, knedp, binding, hasher, sessionmaker):  # noqa: ANN001, ANN201
    desk = MockSigningDesk(knedp, binding, hasher, sessionmaker)
    app = create_app(Settings(environment="test", kep_allow_mock=True), service=service,
                     token_verifier=FakeVerifier(), mock_desk=desk)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c


def auth(world, key: str, suffix: str = "") -> dict[str, str]:  # noqa: ANN001
    return {"Authorization": f"Bearer {world.subs[key]}{suffix}"}


async def test_api_full_chain(client, world, dept, sessionmaker) -> None:  # noqa: ANN001
    doc_id, sha256 = await make_memo(sessionmaker, world, dept.id, dept.author)
    r = await client.post(f"/api/v1/documents/{doc_id}/route-preview", json={}, headers=auth(world, dept.author))
    assert r.status_code == 200, r.text
    assert [s["title_uk"] for s in r.json()["steps"]][-1] == "Планово-фінансовий відділ"

    r = await client.post(f"/api/v1/documents/{doc_id}/routes", json={}, headers=auth(world, dept.author))
    assert r.status_code == 201, r.text
    route = r.json()
    steps = [s["id"] for s in route["steps"]]

    # Проректор намагається завізувати раніше за завідувача.
    r = await client.get(f"/api/v1/steps/{steps[1]}/signing-request", headers=auth(world, "vr_ahr"))
    assert r.status_code == 409 and r.json()["code"] == "invalid_state"

    for step_id, who in zip(steps, [dept.head, "vr_ahr", "chief_acc", "rector", "pfv_head"], strict=True):
        inbox = (await client.get("/api/v1/approval-tasks", headers=auth(world, who))).json()
        assert [i["step_id"] for i in inbox if i["route_id"] == route["id"]] == [step_id]
        req = (await client.get(f"/api/v1/steps/{step_id}/signing-request", headers=auth(world, who))).json()
        assert req["content_sha256"] == sha256
        mock = (await client.post(f"/api/v1/dev/steps/{step_id}/mock-signature", headers=auth(world, who))).json()
        r = await client.post(f"/api/v1/steps/{step_id}/sign", headers=auth(world, who),
                              json={"signature_base64": mock["signature_base64"], "provider": "file_key"})
        assert r.status_code == 200, r.text

    final = (await client.get(f"/api/v1/documents/{doc_id}/route", headers=auth(world, dept.author))).json()
    assert final["status"] == "completed"
    assert all(s["signature"]["validation_status"] == "valid" for s in final["steps"])


async def test_api_errors(client, world, dept, sessionmaker) -> None:  # noqa: ANN001
    doc_id, _ = await make_memo(sessionmaker, world, dept.id, dept.author)
    assert (await client.post(f"/api/v1/documents/{doc_id}/routes", json={})).status_code == 401
    r = await client.post(f"/api/v1/documents/{doc_id}/routes", json={}, headers=auth(world, dept.author, "+ai"))
    assert r.status_code == 403
    r = await client.post(f"/api/v1/documents/{doc_id}/routes", json={"start": True, "extra": 1},
                          headers=auth(world, dept.author))
    assert r.status_code == 422, "зайві поля відхиляються"
    route = (await client.post(f"/api/v1/documents/{doc_id}/routes", json={}, headers=auth(world, dept.author))).json()
    step = route["steps"][0]["id"]

    r = await client.post(f"/api/v1/steps/{step}/sign", headers=auth(world, dept.head),
                          json={"signature_base64": base64.b64encode(b"x" * 40).decode()})
    assert r.status_code == 422 and r.json()["code"] == "signature_rejected"
    assert r.json()["validation_status"] == "invalid"

    r = await client.post(f"/api/v1/steps/{step}/decision", headers=auth(world, dept.head),
                          json={"decision": "rejected"})
    assert r.status_code == 422, "відхилення без причини"
    r = await client.post(f"/api/v1/steps/{step}/decision", headers=auth(world, dept.head),
                          json={"decision": "rejected", "comment": "Перевищено ліміт кошторису"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"


async def test_concurrent_double_sign(service, world, dept, sessionmaker, store) -> None:  # noqa: ANN001
    """Подвійне натискання «Підписати»: проходить рівно одна віза, зайвий контейнер прибрано."""
    from sed_routing import views
    from sed_routing.domain.errors import InvalidState

    from .test_service_pg import sign_req

    doc_id, sha256 = await make_memo(sessionmaker, world, dept.id, dept.author)
    who = Caller(world.subs[dept.author])
    route = await service.create_route(who, doc_id, views.RouteCreate())
    head = Caller(world.subs[dept.head])
    req = sign_req(world, dept.head, sha256)
    results = await asyncio.gather(service.sign(head, route.steps[0].id, req),
                                   service.sign(head, route.steps[0].id, req), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(ok) == 1 and len(errors) == 1 and isinstance(errors[0], InvalidState), results
    assert len(store.objects) == 1
    assert ok[0].current_stage == 2
