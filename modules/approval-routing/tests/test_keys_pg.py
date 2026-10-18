"""Ключі підпису на живому PostgreSQL: закріплення КЕП Дії (ДСТУ 4145) через виклик, внутрішні
ключі АІ-СЕД ПУЕТ (файл-ключ і CSR), візування маршруту різними ключами, відкликання, API."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from sqlalchemy import select

from sed_db.enums import RouteStatus, StepStatus
from sed_db.models import AuditLog, Signature
from sed_routing import views
from sed_routing.adapters.signature_store import InMemoryContentReader
from sed_routing.adapters.sql_kep import SqlInternalCertRegistry, SqlKeyRegistry
from sed_routing.domain.errors import AccessDenied, SignatureRejected
from sed_routing.domain.templates import TemplateCatalog
from sed_routing.kep.cades import build_detached_cades_bes
from sed_routing.kep.challenge import ChallengeIssuer
from sed_routing.kep.internal_ca import InternalCa, bootstrap_ca
from sed_routing.kep.models import PemTrustStore
from sed_routing.kep.registry import FirstMatchBinding, InternalRevocation, RegistryBinding
from sed_routing.kep.verifier import CadesVerifier, InternalTrust
from sed_routing.key_service import KeyManagementService, KeyRegistrationRejected, PrivateKeyUploadRefused
from sed_routing.service import ApprovalService

from .conftest import caller, make_memo
from .dstu_fake import DstuKnedp, FakeDstuSidecar, dstu_sign
from .test_api import FakeVerifier, auth

PWD = "Vizuvannya-2026!"


class Env:
    def __init__(self, sessionmaker, catalog, knedp, binding, hasher, store) -> None:  # noqa: ANN001
        self.diia = DstuKnedp(name="КНЕДП ДІЯ (тест)")
        b = bootstrap_ca()
        self.ca = InternalCa(b.issuing_key, b.issuing_cert, b.root_cert)
        self.keys_reg, self.certs_reg = SqlKeyRegistry(sessionmaker), SqlInternalCertRegistry(sessionmaker)
        self.reader = InMemoryContentReader()
        self.verifier = CadesVerifier(
            PemTrustStore.from_pem(knedp.ca_pem + self.diia.ca_pem), knedp,
            FirstMatchBinding(RegistryBinding(self.keys_reg), binding), hasher,
            internal=InternalTrust(PemTrustStore([self.ca.cert]), InternalRevocation(self.certs_reg)),
            dstu=FakeDstuSidecar(self.diia), allow_mock=True)
        self.store = store
        self.sessionmaker, self.catalog = sessionmaker, catalog
        self.service = self.approval(catalog)
        self.keys = KeyManagementService(sessionmaker, self.verifier, self.keys_reg, self.certs_reg,
                                         ChallengeIssuer(b"c" * 32), self.ca)

    def approval(self, catalog: TemplateCatalog) -> ApprovalService:
        return ApprovalService(self.sessionmaker, catalog, self.verifier, self.store, content_reader=self.reader)


@pytest.fixture()
def env(sessionmaker, catalog, knedp, binding, hasher, store) -> Env:  # noqa: ANN001
    return Env(sessionmaker, catalog, knedp, binding, hasher, store)


def tax_id(world, key: str) -> str:  # noqa: ANN001
    return f"{world.ids[key].int % 10**10:010d}"   # той самий РНОКПП, що в імітаційних ключах conftest


async def register_diia(env: Env, world, key: str, cn: str) -> tuple[bytes, views.KeyBindingView]:  # noqa: ANN001
    cert = env.diia.issue(cn, tax_id(world, key))
    ch = await env.keys.challenge(caller(world, key))
    sig = dstu_sign(cert, base64.b64decode(ch.data_base64))
    b = await env.keys.register(caller(world, key), views.KeyRegisterRequest(
        challenge_token=ch.challenge_token, signature_base64=base64.b64encode(sig).decode(), key_format="key6_dat"))
    return cert, b


def sign_with(key: ec.EllipticCurvePrivateKey, cert: x509.Certificate, sha256: str) -> str:
    p7 = build_detached_cades_bes(cert_der=cert.public_bytes(serialization.Encoding.DER),
                                  content_sha256=bytes.fromhex(sha256),
                                  signing_time=datetime.now(timezone.utc).replace(microsecond=0),
                                  sign=lambda d: key.sign(d, ec.ECDSA(hashes.SHA256())))
    return base64.b64encode(p7).decode()


async def start_memo(env: Env, world, dept) -> tuple[uuid.UUID, str, list]:  # noqa: ANN001
    content = f"%PDF-1.7 memo {uuid.uuid4()}".encode()
    doc_id, sha256 = await make_memo(env.sessionmaker, world, dept.id, dept.author, content=content)
    env.reader.objects[("sed-documents", f"{doc_id}/v1.pdf")] = content
    route = await env.service.create_route(caller(world, dept.author), doc_id, views.RouteCreate())
    return doc_id, sha256, route.steps


# ---------------------------------------------------------------------- закріплення КЕП

async def test_register_diia_key_by_proof_of_possession(env, world, dept, sessionmaker) -> None:  # noqa: ANN001
    cert, b = await register_diia(env, world, dept.head, "Андрій Іванович КОВАЛЬ")
    assert b.level == "qualified" and b.bound_via == "proof_of_possession" and b.key_format == "key6_dat"
    again = await env.keys.register(caller(world, dept.head), views.KeyRegisterRequest(
        challenge_token=(ch := await env.keys.challenge(caller(world, dept.head))).challenge_token,
        signature_base64=base64.b64encode(dstu_sign(cert, base64.b64decode(ch.data_base64))).decode()))
    assert again.id == b.id                                # повторна реєстрація — без дубля
    assert [k.id for k in await env.keys.my_keys(caller(world, dept.head))] == [b.id]
    async with sessionmaker() as s:
        assert await s.scalar(select(AuditLog.id).where(AuditLog.action == "kep.key_registered",
                                                        AuditLog.entity_id == b.id))
    # Той самий сертифікат не можна закріпити за іншою людиною.
    lax = KeyManagementService(sessionmaker, env.verifier, env.keys_reg, env.certs_reg,
                               ChallengeIssuer(b"c" * 32), env.ca, require_name_match=False)
    ch = await lax.challenge(caller(world, dept.author))
    with pytest.raises(KeyRegistrationRejected, match="іншим користувачем"):
        await lax.register(caller(world, dept.author), views.KeyRegisterRequest(
            challenge_token=ch.challenge_token,
            signature_base64=base64.b64encode(dstu_sign(cert, base64.b64decode(ch.data_base64))).decode()))


async def test_registration_rejections(env, world, dept) -> None:  # noqa: ANN001
    me = caller(world, dept.author)
    cert = env.diia.issue("Олена ПЕТРЕНКО", tax_id(world, dept.author))
    ch = await env.keys.challenge(me)
    other = await env.keys.challenge(caller(world, dept.head))
    with pytest.raises(KeyRegistrationRejected, match="іншому користувачу"):   # чужий виклик
        await env.keys.register(me, views.KeyRegisterRequest(
            challenge_token=other.challenge_token,
            signature_base64=base64.b64encode(dstu_sign(cert, base64.b64decode(other.data_base64))).decode()))
    with pytest.raises(SignatureRejected):                                       # підписано не виклик
        await env.keys.register(me, views.KeyRegisterRequest(
            challenge_token=ch.challenge_token,
            signature_base64=base64.b64encode(dstu_sign(cert, b"something else")).decode()))
    stranger = env.diia.issue("Марія СИДОРЕНКО", tax_id(world, dept.author))
    with pytest.raises(KeyRegistrationRejected, match="ПІБ"):
        await env.keys.register(me, views.KeyRegisterRequest(
            challenge_token=ch.challenge_token,
            signature_base64=base64.b64encode(dstu_sign(stranger, base64.b64decode(ch.data_base64))).decode()))


async def test_inspect_certificate_and_refuse_private_keys(env, world, dept) -> None:  # noqa: ANN001
    me = caller(world, dept.author)
    cert = env.diia.issue("Олена ПЕТРЕНКО", "3012345678")
    rep = await env.keys.inspect_certificate(me, cert)
    assert rep.level == "qualified" and rep.trusted_issuer is None and rep.usable_for_signing
    assert rep.certificate["tax_id_masked"] == "******5678" and "3012345678" not in str(rep.certificate)
    blob, _ = env.ca.generate_key_file(__import__("sed_routing.kep.internal_ca", fromlist=["x"]).InternalSubject(
        uuid.uuid4(), "Тест ТЕСТ"), PWD)
    with pytest.raises(PrivateKeyUploadRefused):
        await env.keys.inspect_certificate(me, blob)


# ---------------------------------------------------------------------- маршрут різними ключами

async def test_route_signed_with_diia_file_key_and_internal_keys(env, world, dept, sessionmaker) -> None:  # noqa: ANN001
    _, sha256, steps = await start_memo(env, world, dept)
    # 1) Зав. кафедри — файл-ключ Дії (ДСТУ 4145), перевіряє сертифікований провайдер.
    cert, _ = await register_diia(env, world, dept.head, "Андрій КОВАЛЬ")
    req = await env.service.signing_request(caller(world, dept.head), steps[0].id)
    assert req.accepted_levels == ["qualified", "internal"]
    content = env.reader.objects[("sed-documents", f"{req.document_id}/v1.pdf")]
    route = await env.service.sign(caller(world, dept.head), steps[0].id, views.SignRequest(
        signature_base64=base64.b64encode(dstu_sign(cert, content)).decode()))
    assert route.steps[0].status is StepStatus.APPROVED and route.steps[1].status is StepStatus.ACTIVE
    # 2) Проректор — внутрішній файл-ключ АІ-СЕД ПУЕТ (PKCS#12, виданий СЕД).
    blob, name = await env.keys.issue_key_file(caller(world, "vr_ahr"), views.KeyFileRequest(password=PWD))
    assert name.endswith(".puet-sed.p12")
    key, icert, _ = pkcs12.load_key_and_certificates(blob, PWD.encode())
    route = await env.service.sign(caller(world, "vr_ahr"), steps[1].id,
                                   views.SignRequest(signature_base64=sign_with(key, icert, sha256)))
    assert route.steps[1].status is StepStatus.APPROVED
    # 3) Головний бухгалтер — внутрішній ключ за CSR (ключ не залишав «комп'ютер»).
    k3 = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([]))
           .sign(k3, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode())
    issued = await env.keys.enroll_csr(caller(world, "chief_acc"), views.CsrEnrollRequest(csr_pem=csr))
    c3 = x509.load_pem_x509_certificate(issued.certificate_pem.encode())
    route = await env.service.sign(caller(world, "chief_acc"), steps[2].id,
                                   views.SignRequest(signature_base64=sign_with(k3, c3, sha256)))
    assert route.steps[3].status is StepStatus.ACTIVE
    async with sessionmaker() as s:
        sigs = list(await s.scalars(select(Signature).where(Signature.approval_step_id.in_(
            [steps[0].id, steps[1].id, steps[2].id])).order_by(Signature.signed_at)))
    levels = {x.approval_step_id: x.validation_report["level"] for x in sigs}
    assert levels == {steps[0].id: "qualified", steps[1].id: "internal", steps[2].id: "internal"}
    assert all(x.signer_tax_id_hmac is None for x in sigs if x.validation_report["level"] == "internal")
    assert next(x for x in sigs if x.approval_step_id == steps[0].id).signer_tax_id_hmac is not None
    # 4–5) Решта — імітаційним КЕП; маршрут завершується.
    for i, k in ((3, "rector"), (4, "pfv_head")):
        route = await env.service.sign(caller(world, k), steps[i].id, views.SignRequest(
            signature_base64=base64.b64encode(world.keys[k].sign(sha256)).decode()))
    assert route.status is RouteStatus.COMPLETED


async def test_internal_key_refused_where_template_requires_kep(env, world, dept, catalog) -> None:  # noqa: ANN001
    spec = catalog.get("MEMO_PROCUREMENT")
    strict = TemplateCatalog([spec.model_copy(update={"steps": tuple(
        s.model_copy(update={"signature_levels": ("qualified",)}) for s in spec.steps)})])
    env.service = env.approval(strict)
    _, sha256, steps = await start_memo(env, world, dept)
    blob, _ = await env.keys.issue_key_file(caller(world, dept.head), views.KeyFileRequest(password=PWD))
    key, icert, _ = pkcs12.load_key_and_certificates(blob, PWD.encode())
    assert (await env.service.signing_request(caller(world, dept.head), steps[0].id)).accepted_levels == ["qualified"]
    with pytest.raises(SignatureRejected, match="кваліфікованого КЕП"):
        await env.service.sign(caller(world, dept.head), steps[0].id,
                               views.SignRequest(signature_base64=sign_with(key, icert, sha256)))
    route = await env.service.get_route(caller(world, dept.head), (await env.service.current_route_id(
        caller(world, dept.author), (await env.service.signing_request(caller(world, dept.head), steps[0].id)).document_id)))
    assert route.steps[0].status is StepStatus.ACTIVE and route.steps[1].status is StepStatus.WAITING


# ---------------------------------------------------------------------- відкликання

async def test_reissue_supersedes_and_revocation_blocks_signing(env, world, dept) -> None:  # noqa: ANN001
    me = caller(world, dept.head)
    _, sha256, steps = await start_memo(env, world, dept)
    old_blob, _ = await env.keys.issue_key_file(me, views.KeyFileRequest(password=PWD))
    new_blob, _ = await env.keys.issue_key_file(me, views.KeyFileRequest(password=PWD))
    old_key, old_cert, _ = pkcs12.load_key_and_certificates(old_blob, PWD.encode())
    with pytest.raises(SignatureRejected, match="відкликано"):
        await env.service.sign(me, steps[0].id, views.SignRequest(signature_base64=sign_with(old_key, old_cert, sha256)))
    crl = x509.load_der_x509_crl(await env.keys.crl())
    assert crl.get_revoked_certificate_by_serial_number(old_cert.serial_number) is not None
    active = [k for k in await env.keys.my_keys(me) if k.level == "internal"]
    assert len(active) == 1
    # Чужий ключ відкликати не можна; адміністратор ключів — може.
    with pytest.raises(AccessDenied):
        await env.keys.revoke_key(caller(world, dept.author), active[0].id, views.KeyRevokeRequest(reason="помилка"))
    await env.keys.revoke_key(caller(world, dept.author, "sed-kep-admin"), active[0].id,
                              views.KeyRevokeRequest(reason="компрометація ключа"))
    new_key, new_cert, _ = pkcs12.load_key_and_certificates(new_blob, PWD.encode())
    with pytest.raises(SignatureRejected, match="відкликано"):
        await env.service.sign(me, steps[0].id, views.SignRequest(signature_base64=sign_with(new_key, new_cert, sha256)))


# ---------------------------------------------------------------------- HTTP API

@pytest.fixture()
async def kclient(env):  # noqa: ANN001, ANN201
    from sed_routing.api.app import create_app
    from sed_routing.config import Settings

    app = create_app(Settings(environment="test"), service=env.service, keys=env.keys, token_verifier=FakeVerifier())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def test_key_api(kclient, env, world, dept) -> None:  # noqa: ANN001
    h = auth(world, dept.author)
    r = await kclient.post("/api/v1/kep/challenges", headers=h)
    assert r.status_code == 200 and "AI-SED PUET" in r.json()["data_text"]
    cert = env.diia.issue("Олена ПЕТРЕНКО", tax_id(world, dept.author))
    sig = dstu_sign(cert, base64.b64decode(r.json()["data_base64"]))
    r = await kclient.post("/api/v1/kep/keys", headers=h, json={
        "challenge_token": r.json()["challenge_token"], "signature_base64": base64.b64encode(sig).decode(),
        "key_format": "jks"})
    assert r.status_code == 201, r.text
    assert [k["level_uk"] for k in (await kclient.get("/api/v1/kep/keys", headers=h)).json()] == ["Кваліфікований (КЕП)"]
    r = await kclient.post("/api/v1/kep/certificates/inspect", headers=h, files={"file": ("me.cer", cert)})
    assert r.status_code == 200 and r.json()["registered_to_me"] is True
    r = await kclient.post("/api/v1/kep/internal/key-file", headers=h, json={"password": PWD})
    assert r.status_code == 201 and r.headers["content-type"] == "application/x-pkcs12"
    assert r.headers["cache-control"] == "no-store" and ".puet-sed.p12" in r.headers["content-disposition"]
    r2 = await kclient.post("/api/v1/kep/certificates/inspect", headers=h, files={"file": ("k.p12", r.content)})
    assert r2.status_code == 422 and r2.json()["code"] == "private_key_upload_refused"
    r = await kclient.post("/api/v1/kep/internal/key-file", headers=h, json={"password": "weakpassword"})
    assert r.status_code == 422 and "Слабкий пароль" in r.json()["detail"]
    assert (await kclient.get("/api/v1/kep/internal/ca.pem")).text.count("BEGIN CERTIFICATE") == 2
    assert x509.load_der_x509_crl((await kclient.get("/api/v1/kep/internal/crl")).content)
    r = await kclient.post("/api/v1/kep/signatures/verify", headers=h, json={
        "signature_base64": base64.b64encode(world.keys[dept.author].sign("ab" * 32)).decode(),
        "content_sha256": "ab" * 32})
    assert r.json()["status"] == "valid" and r.json()["level"] == "mock"
