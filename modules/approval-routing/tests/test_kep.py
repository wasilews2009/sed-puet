"""Перевірка КЕП: імітаційний КНЕДП підписує справжнім CAdES-BES, верифікатор ловить підробки."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from asn1crypto import cms

from sed_db.enums import SignatureFormat, SignatureValidation
from sed_routing.kep.mock import MockKnedp
from sed_routing.kep.models import PemTrustStore, StaticCertificateBinding, TaxIdHasher
from sed_routing.kep.verifier import CadesVerifier

from .conftest import HMAC_SECRET, sha

DOC = b"%PDF-1.7 sluzhbova zapyska"
DOC_SHA = sha(DOC)
TAX_ID = "3012345678"


@pytest.fixture()
def env():  # noqa: ANN201
    knedp = MockKnedp()
    hasher = TaxIdHasher(HMAC_SECRET)
    user = uuid.uuid4()
    binding = StaticCertificateBinding({user: hasher(TAX_ID)})
    key = knedp.issue("Андрій КОВАЛЬ", TAX_ID)

    def verifier(**kw) -> CadesVerifier:  # noqa: ANN003
        trust = kw.pop("trust", PemTrustStore.from_pem(knedp.ca_pem))
        return CadesVerifier(trust, knedp, binding, hasher, **{"allow_mock": True, **kw})

    return knedp, key, user, verifier


async def check(verifier: CadesVerifier, container: bytes, user: uuid.UUID, **kw):  # noqa: ANN003, ANN201
    return await verifier.verify(container, expected_sha256=kw.pop("expected", DOC_SHA), signer_user_id=user,
                                 not_before=kw.pop("not_before", None), **kw)


async def test_valid_cades_bes(env) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    r = await check(verifier(), key.sign(DOC_SHA), user)
    assert r.ok, r.reasons
    assert r.signature_format is SignatureFormat.CADES_BES
    assert r.certificate.subject_cn == "Андрій КОВАЛЬ" and r.certificate.is_qualified
    assert TAX_ID not in str(r.report()), "РНОКПП не потрапляє у звіт валідації"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl не встановлено")
def test_container_verifies_with_openssl(env, tmp_path: Path) -> None:  # noqa: ANN001
    """Незалежна перевірка: контейнер приймає OpenSSL CMS як відокремлений підпис."""
    knedp, key, _, _ = env
    (tmp_path / "doc.pdf").write_bytes(DOC)
    (tmp_path / "sig.p7s").write_bytes(key.sign(DOC_SHA))
    (tmp_path / "ca.pem").write_bytes(knedp.ca_pem)
    out = subprocess.run(
        ["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-in", str(tmp_path / "sig.p7s"),
         "-content", str(tmp_path / "doc.pdf"), "-CAfile", str(tmp_path / "ca.pem"), "-purpose", "any",
         "-out", "/dev/null"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    (tmp_path / "doc.pdf").write_bytes(DOC + b"!")
    tampered = subprocess.run(
        ["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-in", str(tmp_path / "sig.p7s"),
         "-content", str(tmp_path / "doc.pdf"), "-CAfile", str(tmp_path / "ca.pem"), "-purpose", "any",
         "-out", "/dev/null"], capture_output=True, text=True)
    assert tampered.returncode != 0


async def test_signature_over_other_document(env) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    r = await check(verifier(), key.sign(sha(b"other")), user)
    assert r.status is SignatureValidation.INVALID and "Геш" in r.reasons[0]


async def test_tampered_signature_bytes(env) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    ci = cms.ContentInfo.load(key.sign(DOC_SHA))
    si = ci["content"]["signer_infos"][0]
    sig = bytearray(si["signature"].native)
    sig[-1] ^= 0x01
    si["signature"] = bytes(sig)
    r = await check(verifier(), ci.dump(force=True), user)
    assert r.status is SignatureValidation.INVALID and "математично" in r.reasons[0]


async def test_untrusted_issuer(env) -> None:  # noqa: ANN001
    _, _, user, verifier = env
    rogue = MockKnedp(name="Шахрайський ЦСК")
    r = await check(verifier(), rogue.issue("Андрій КОВАЛЬ", TAX_ID).sign(DOC_SHA), user)
    assert r.status is SignatureValidation.INVALID and "недовіреним" in r.reasons[0]


async def test_mock_rejected_when_not_allowed(env) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    r = await check(verifier(allow_mock=False), key.sign(DOC_SHA), user)
    assert r.status is SignatureValidation.INVALID and "імітаційного" in r.reasons[0]


async def test_revoked_and_ocsp_unavailable(env) -> None:  # noqa: ANN001
    knedp, key, user, verifier = env
    knedp.ocsp_available = False
    r = await check(verifier(), key.sign(DOC_SHA), user)
    assert r.status is SignatureValidation.INDETERMINATE, "недоступний OCSP ≠ валідний підпис"
    knedp.ocsp_available = True
    knedp.revoke(key)
    r = await check(verifier(), key.sign(DOC_SHA), user)
    assert r.status is SignatureValidation.CERT_REVOKED


async def test_expired_certificate(env) -> None:  # noqa: ANN001
    knedp, _, user, verifier = env
    past = datetime.now(timezone.utc) - timedelta(days=400)
    old = knedp.issue("Андрій КОВАЛЬ", TAX_ID, valid_from=past, valid_to=past + timedelta(days=365))
    r = await check(verifier(), old.sign(DOC_SHA), user)
    assert r.status is SignatureValidation.CERT_EXPIRED


async def test_not_qualified_and_wrong_key_usage(env) -> None:  # noqa: ANN001
    knedp, _, user, verifier = env
    r = await check(verifier(), knedp.issue("Андрій КОВАЛЬ", TAX_ID, qualified=False).sign(DOC_SHA), user)
    assert "не кваліфікований" in r.reasons[0]
    r = await check(verifier(), knedp.issue("Андрій КОВАЛЬ", TAX_ID, key_usage_signing=False).sign(DOC_SHA), user)
    assert "не призначений" in r.reasons[0]


async def test_certificate_of_another_person(env) -> None:  # noqa: ANN001
    knedp, _, user, verifier = env
    r = await check(verifier(), knedp.issue("Іван РЕКТОРЕНКО", "1111111111").sign(DOC_SHA), user)
    assert r.status is SignatureValidation.INVALID and "іншій особі" in r.reasons[0]
    r = await check(verifier(), knedp.issue("Хтось", "1111111111").sign(DOC_SHA), uuid.uuid4())
    assert "не закріплено" in r.reasons[0]


async def test_signing_time_rules(env) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    now = datetime.now(timezone.utc)
    early = key.sign(DOC_SHA, now - timedelta(hours=1))
    r = await check(verifier(), early, user, not_before=now - timedelta(minutes=10))
    assert "раніше, ніж документ надійшов" in r.reasons[0]
    r = await check(verifier(), key.sign(DOC_SHA, now + timedelta(hours=1)), user)
    assert "майбутньому" in r.reasons[0]


@pytest.mark.parametrize("mutate, reason", [
    (lambda c: b"not a cms", "ASN.1"),
    (lambda c: c[:-10], "ASN.1"),
    (lambda c: b"", "Розмір"),
])
async def test_garbage_containers(env, mutate, reason) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    r = await check(verifier(), mutate(key.sign(DOC_SHA)), user)
    assert r.status is SignatureValidation.INVALID and reason in r.reasons[0]


async def test_attached_content_and_missing_ess_attr_rejected(env) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    ci = cms.ContentInfo.load(key.sign(DOC_SHA))
    ci["content"]["encap_content_info"]["content"] = DOC
    r = await check(verifier(), ci.dump(force=True), user)
    assert "відокремлений" in r.reasons[0]

    ci = cms.ContentInfo.load(key.sign(DOC_SHA))
    si = ci["content"]["signer_infos"][0]
    si["signed_attrs"] = cms.CMSAttributes([a for a in si["signed_attrs"] if a["type"].native != "signing_certificate_v2"])
    r = await check(verifier(), ci.dump(force=True), user)
    assert "signing-certificate-v2" in r.reasons[0]


async def test_dstu_algorithms_need_certified_provider(env) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    ci = cms.ContentInfo.load(key.sign(DOC_SHA))
    ci["content"]["signer_infos"][0]["digest_algorithm"]["algorithm"] = "1.2.804.2.1.1.1.1.2.1"
    r = await check(verifier(), ci.dump(force=True), user)
    assert r.status is SignatureValidation.INDETERMINATE and "ГОСТ 34.311" in r.reasons[0]


async def test_oversized_container(env) -> None:  # noqa: ANN001
    _, key, user, verifier = env
    r = await check(verifier(max_container_bytes=100), key.sign(DOC_SHA), user)
    assert "Розмір" in r.reasons[0]


def test_tax_id_hasher_requires_strong_secret() -> None:
    with pytest.raises(ValueError):
        TaxIdHasher(b"short")
