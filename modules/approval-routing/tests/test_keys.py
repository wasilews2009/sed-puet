"""Ключі підпису без БД: файл-ключі (PKCS#12, JKS ПриватБанку), сертифікати ДСТУ (Дія), внутрішній
ЦСК АІ-СЕД ПУЕТ, виклики для доказу володіння, політика рівнів підпису, офлайн-утиліта keytool."""

from __future__ import annotations

import base64
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import ValidationError

from sed_db.enums import SignatureValidation
from sed_routing.domain.templates import StepSpec
from sed_routing.kep.cades import build_detached_cades_bes
from sed_routing.kep.certinfo import CertLevel, analyze_certificate
from sed_routing.kep.challenge import ChallengeError, ChallengeIssuer
from sed_routing.kep.internal_ca import (
    EnrollmentError,
    InternalCa,
    InternalSubject,
    bootstrap_ca,
    check_password_strength,
)
from sed_routing.kep.keyfile import KeyFormat, detect_format, inspect_key_file, looks_like_key_container
from sed_routing.kep.mock import MockKnedp
from sed_routing.kep.models import PemTrustStore, StaticCertificateBinding, TaxIdHasher
from sed_routing.kep.registry import (
    InMemoryInternalCertRegistry,
    InMemoryKeyRegistry,
    InternalCertRecord,
    InternalRevocation,
    KeyBinding,
    RegistryBinding,
)
from sed_routing.kep.verifier import CadesVerifier, InternalTrust
from sed_routing.service import _enforce_level

from .conftest import HMAC_SECRET, sha
from .dstu_fake import DstuKnedp, FakeDstuSidecar, dstu_sign

DOC = b"%PDF-1.7 sluzhbova zapyska pro zakupivlyu"
DOC_SHA = sha(DOC)
PWD = "Kafedra-2026!x"
HASHER = TaxIdHasher(HMAC_SECRET)


async def _content() -> bytes:
    return DOC


# ---------------------------------------------------------------------- ДСТУ 4145 (Дія, ПриватБанк)

@pytest.fixture()
def dstu():  # noqa: ANN201
    knedp = DstuKnedp()
    user = uuid.uuid4()
    binding = StaticCertificateBinding({user: HASHER("3012345678")})
    cert = knedp.issue("Олена Петрівна ПЕТРЕНКО", "3012345678")
    sidecar = FakeDstuSidecar(knedp)

    def verifier(**kw) -> CadesVerifier:  # noqa: ANN003
        return CadesVerifier(PemTrustStore.from_pem(kw.pop("trust_pem", knedp.ca_pem)), MockKnedp(), binding, HASHER,
                             **{"dstu": sidecar, **kw})

    return knedp, cert, user, verifier, sidecar


def test_dstu_certificate_is_recognised_with_tax_id() -> None:
    k = DstuKnedp()
    for via_sn in (True, False):  # TINUA- у serialNumber або DRFO у subjectDirectoryAttributes
        rep = analyze_certificate(k.issue("Іван ІВАНЕНКО", "2233445566", via_serial_number=via_sn))
        assert rep.is_dstu and rep.key_algorithm == "ДСТУ 4145-2002"
        assert rep.level is CertLevel.QUALIFIED and rep.tax_id == "2233445566"
        pub = rep.public_dict()
        assert pub["tax_id_masked"] == "******5566" and "2233445566" not in str(pub)
    assert analyze_certificate(k.ca_der).is_ca


async def test_dstu_signature_verified_by_certified_provider(dstu) -> None:  # noqa: ANN001
    _, cert, user, verifier, sidecar = dstu
    r = await verifier().verify(dstu_sign(cert, DOC), expected_sha256=DOC_SHA, signer_user_id=user,
                                not_before=None, content=_content)
    assert r.ok, r.reasons
    assert r.level is CertLevel.QUALIFIED and sidecar.calls == 1
    assert r.checks["signature"] == "ok (сертифікований провайдер)"


async def test_dstu_without_provider_is_indeterminate(dstu) -> None:  # noqa: ANN001
    _, cert, user, verifier, _ = dstu
    r = await verifier(dstu=None).verify(dstu_sign(cert, DOC), expected_sha256=DOC_SHA, signer_user_id=user,
                                         not_before=None, content=_content)
    assert r.status is SignatureValidation.INDETERMINATE and "ІІТ" in r.reasons[0]


async def test_dstu_provider_down_is_indeterminate(dstu) -> None:  # noqa: ANN001
    _, cert, user, verifier, sidecar = dstu
    sidecar.down = True
    r = await verifier().verify(dstu_sign(cert, DOC), expected_sha256=DOC_SHA, signer_user_id=user,
                                not_before=None, content=_content)
    assert r.status is SignatureValidation.INDETERMINATE and "недоступний" in r.reasons[0]


async def test_dstu_rejections(dstu) -> None:  # noqa: ANN001
    knedp, cert, user, verifier, _ = dstu
    v = verifier()
    r = await v.verify(dstu_sign(cert, b"other document"), expected_sha256=DOC_SHA, signer_user_id=user,
                       not_before=None, content=_content)
    assert r.status is SignatureValidation.INVALID  # підпис над іншим вмістом
    alien = DstuKnedp(name="Невідомий ЦСК")
    r = await v.verify(dstu_sign(alien.issue("Олена ПЕТРЕНКО", "3012345678"), DOC), expected_sha256=DOC_SHA,
                       signer_user_id=user, not_before=None, content=_content)
    assert r.status is SignatureValidation.INVALID and "недовіреним" in r.reasons[0]
    r = await v.verify(dstu_sign(knedp.issue("Інша ОСОБА", "1111111111"), DOC), expected_sha256=DOC_SHA,
                       signer_user_id=user, not_before=None, content=_content)
    assert r.status is SignatureValidation.INVALID and "іншій особі" in r.reasons[0]
    knedp.revoked.add(analyze_certificate(cert).serial_hex)
    r = await v.verify(dstu_sign(cert, DOC), expected_sha256=DOC_SHA, signer_user_id=user,
                       not_before=None, content=_content)
    assert r.status is SignatureValidation.CERT_REVOKED


# ---------------------------------------------------------------------- файл-ключі

def test_key_container_detection(tmp_path: Path) -> None:
    ca = InternalCa(*_ca_parts())
    blob, cert = ca.generate_key_file(_subject(), PWD)
    assert detect_format(blob) is KeyFormat.PKCS12 and looks_like_key_container(blob)
    der = cert.public_bytes(serialization.Encoding.DER)
    assert detect_format(der) is KeyFormat.CERTIFICATE and not looks_like_key_container(der)
    good = inspect_key_file(blob, PWD)
    assert good.ok and good.password_ok and good.key_matches_certificate and good.can_sign_locally
    assert good.signer_certificate is not None and good.signer_certificate.level is CertLevel.INTERNAL
    bad = inspect_key_file(blob, "wrong-password")
    assert bad.password_ok is False and not bad.ok


@pytest.mark.skipif(shutil.which("keytool") is None, reason="немає Java keytool")
def test_privatbank_style_jks(tmp_path: Path) -> None:
    """ПриватБанк видає ключ у сховищі Java (JKS). Перевіряємо пароль і сертифікат без JVM."""
    jks = tmp_path / "privat.jks"
    subprocess.run(["keytool", "-genkeypair", "-keystore", str(jks), "-storetype", "JKS", "-storepass", PWD,
                    "-keypass", PWD, "-alias", "key", "-keyalg", "EC", "-groupname", "secp256r1",
                    "-dname", "CN=Petrenko Olena,C=UA", "-validity", "30"], check=True, capture_output=True)
    data = jks.read_bytes()
    assert detect_format(data) is KeyFormat.JKS and looks_like_key_container(data)
    rep = inspect_key_file(data, PWD)
    assert rep.password_ok and rep.key_matches_certificate and rep.certificates
    assert inspect_key_file(data, "bad-password-1").password_ok is False


# ---------------------------------------------------------------------- внутрішній ЦСК

def _ca_parts() -> tuple:
    b = bootstrap_ca()
    return b.issuing_key, b.issuing_cert, b.root_cert


def _subject(uid: uuid.UUID | None = None) -> InternalSubject:
    return InternalSubject(uid or uuid.uuid4(), "Андрій Іванович КОВАЛЬ", "a.koval@puet.edu.ua", "Кафедра обліку")


def test_password_policy() -> None:
    s = _subject()
    assert check_password_strength("short", s)
    assert check_password_strength("koval-Kafedra-2026", s)  # прізвище в паролі
    assert not check_password_strength(PWD, s)
    with pytest.raises(EnrollmentError):
        InternalCa(*_ca_parts()).generate_key_file(s, "alllowercase-long")


def test_internal_certificate_profile() -> None:
    ca = InternalCa(*_ca_parts())
    s = _subject()
    _, cert = ca.generate_key_file(s, PWD)
    rep = analyze_certificate(cert.public_bytes(serialization.Encoding.DER))
    assert rep.level is CertLevel.INTERNAL and rep.sed_user_id == s.user_id and not rep.is_qualified
    assert rep.subject_cn == "Андрій Іванович КОВАЛЬ"
    cert.verify_directly_issued_by(ca.cert)


def test_csr_subject_is_ignored_and_weak_keys_refused() -> None:
    ca = InternalCa(*_ca_parts())
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Ректор")]))
           .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    cert = ca.issue_from_csr(csr, _subject())
    assert cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value == "Андрій Іванович КОВАЛЬ"
    weak = rsa.generate_private_key(65537, 2048)
    csr2 = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([]))
            .sign(weak, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    with pytest.raises(EnrollmentError, match="3072"):
        ca.issue_from_csr(csr2, _subject())
    with pytest.raises(EnrollmentError):
        ca.issue_from_csr(b"-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n-----END CERTIFICATE REQUEST-----\n", _subject())


@pytest.fixture()
def internal_env():  # noqa: ANN201
    ca = InternalCa(*_ca_parts())
    reg = InMemoryInternalCertRegistry()
    user = uuid.uuid4()
    key = ec.generate_private_key(ec.SECP256R1())
    cert = ca.issue(_subject(user), key.public_key())
    der = cert.public_bytes(serialization.Encoding.DER)
    rep = analyze_certificate(der)
    now = datetime.now(timezone.utc)

    async def register() -> None:
        await reg.add(InternalCertRecord(rep.serial_hex, user, rep.fingerprint_sha256, der, rep.valid_from,
                                         rep.valid_to, "csr", now))

    def sign(sha256: str = DOC_SHA) -> bytes:
        return build_detached_cades_bes(cert_der=der, content_sha256=bytes.fromhex(sha256),
                                        signing_time=now.replace(microsecond=0),
                                        sign=lambda d: key.sign(d, ec.ECDSA(hashes.SHA256())))

    knedp = MockKnedp()
    verifier = CadesVerifier(PemTrustStore.from_pem(knedp.ca_pem), knedp, StaticCertificateBinding(), HASHER,
                             internal=InternalTrust(PemTrustStore([ca.cert]), InternalRevocation(reg)), allow_mock=True)
    return ca, reg, user, rep, register, sign, verifier


async def test_internal_signature_levels(internal_env) -> None:  # noqa: ANN001
    ca, reg, user, rep, register, sign, verifier = internal_env
    r = await verifier.verify(sign(), expected_sha256=DOC_SHA, signer_user_id=user, not_before=None)
    assert r.status is SignatureValidation.INVALID and "реєстрі" in r.reasons[0]  # ще не видано через СЕД
    await register()
    r = await verifier.verify(sign(), expected_sha256=DOC_SHA, signer_user_id=user, not_before=None)
    assert r.ok and r.level is CertLevel.INTERNAL
    r = await verifier.verify(sign(), expected_sha256=DOC_SHA, signer_user_id=uuid.uuid4(), not_before=None)
    assert r.status is SignatureValidation.INVALID and "іншому користувачу" in r.reasons[0]
    await reg.revoke(rep.serial_hex, datetime.now(timezone.utc), "key_compromise", user)
    r = await verifier.verify(sign(), expected_sha256=DOC_SHA, signer_user_id=user, not_before=None)
    assert r.status is SignatureValidation.CERT_REVOKED
    crl = x509.load_der_x509_crl(await _crl(ca, reg))
    assert crl.is_signature_valid(ca.cert.public_key())
    assert crl.get_revoked_certificate_by_serial_number(int(rep.serial_hex, 16)) is not None


async def _crl(ca: InternalCa, reg: InMemoryInternalCertRegistry) -> bytes:
    return ca.crl([(int(r.serial_hex, 16), r.revoked_at, x509.ReasonFlags.key_compromise)
                   for r in await reg.revoked() if r.revoked_at])


async def test_internal_ca_not_trusted_without_configuration(internal_env) -> None:  # noqa: ANN001
    _, _, user, _, register, sign, _ = internal_env
    await register()
    knedp = MockKnedp()
    plain = CadesVerifier(PemTrustStore.from_pem(knedp.ca_pem), knedp, StaticCertificateBinding(), HASHER,
                          allow_mock=True)
    r = await plain.verify(sign(), expected_sha256=DOC_SHA, signer_user_id=user, not_before=None)
    assert r.status is SignatureValidation.INVALID and "недовіреним" in r.reasons[0]


# ---------------------------------------------------------------------- політика рівнів

async def test_level_policy_per_step(internal_env) -> None:  # noqa: ANN001
    _, _, user, _, register, sign, verifier = internal_env
    await register()
    r = await verifier.verify(sign(), expected_sha256=DOC_SHA, signer_user_id=user, not_before=None)
    assert _enforce_level(r, ("qualified", "internal")).ok
    denied = _enforce_level(r, ("qualified",))
    assert denied.status is SignatureValidation.INVALID and "кваліфікованого КЕП" in denied.reasons[0]
    knedp = MockKnedp()
    key = knedp.issue("Андрій КОВАЛЬ", "3012345678")
    mock = await CadesVerifier(PemTrustStore.from_pem(knedp.ca_pem), knedp,
                               StaticCertificateBinding({user: HASHER("3012345678")}), HASHER, allow_mock=True
                               ).verify(key.sign(DOC_SHA), expected_sha256=DOC_SHA, signer_user_id=user, not_before=None)
    assert mock.level is CertLevel.MOCK and _enforce_level(mock, ("qualified",)).ok


def test_signing_step_requires_qualified_only() -> None:
    base = dict(title_uk="Ректор", kind="signing", rule="role", role="rector", requires_kep=True)
    StepSpec.model_validate(base)
    with pytest.raises(ValidationError, match="кваліфікованим"):
        StepSpec.model_validate({**base, "signature_levels": ["qualified", "internal"]})
    assert StepSpec.model_validate({**base, "kind": "approval"}).signature_levels == ("qualified",)


# ---------------------------------------------------------------------- закріплення ключів

async def test_registry_binding_semantics() -> None:
    reg = InMemoryKeyRegistry()
    rb = RegistryBinding(reg)
    me, other = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    assert await rb.matches(me, "a" * 64, HASHER("1")) is None
    b = await reg.add(KeyBinding(me, CertLevel.QUALIFIED, "a" * 64, "01", "КНЕДП", "Я", now, now + timedelta(days=1),
                                 "proof_of_possession", tax_id_hmac=HASHER("1")))
    assert await rb.matches(me, "a" * 64, HASHER("1")) is True
    assert await rb.matches(other, "a" * 64, HASHER("1")) is False
    assert await rb.matches(me, "b" * 64, HASHER("1")) is True    # перевипущений сертифікат тієї ж особи
    assert await rb.matches(me, "b" * 64, HASHER("2")) is False
    await reg.revoke(b.id, now, "втрачено")
    assert await rb.matches(me, "a" * 64, HASHER("1")) is None


def test_challenge_tokens() -> None:
    issuer = ChallengeIssuer(b"k" * 32)
    me = uuid.uuid4()
    c = issuer.issue(me)
    assert issuer.open(c.token, me).data == c.data and str(me).encode() in c.data
    with pytest.raises(ChallengeError):
        issuer.open(c.token, uuid.uuid4())
    with pytest.raises(ChallengeError):
        issuer.open(c.token, me, "other-purpose")
    with pytest.raises(ChallengeError, match="Строк"):
        issuer.open(c.token, me, now=datetime.now(timezone.utc) + timedelta(hours=1))
    raw, mac = c.token.split(".")
    with pytest.raises(ChallengeError):
        issuer.open(raw[:-2] + "AA." + mac, me)
    with pytest.raises(ValueError):
        ChallengeIssuer(b"short")


# ---------------------------------------------------------------------- офлайн-утиліта keytool

def test_keytool_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from sed_routing import keytool

    monkeypatch.setenv("KP", PWD)
    monkeypatch.setenv("RP", "Root-Ceremony-Passw0rd!")
    monkeypatch.setenv("IP", "Issuing-Ceremony-Passw0rd!")
    assert keytool.main(["ca-init", "--out", str(tmp_path / "ca"), "--password-env", "RP",
                         "--issuing-password-env", "IP"]) == 0
    ca = InternalCa.from_pem((tmp_path / "ca/issuing.key.pem").read_bytes(), b"Issuing-Ceremony-Passw0rd!",
                             (tmp_path / "ca/chain.pem").read_bytes())
    assert keytool.main(["new-key", "--key", str(tmp_path / "me.key"), "--csr", str(tmp_path / "me.csr"),
                         "--password-env", "KP"]) == 0
    assert (tmp_path / "me.key").stat().st_mode & 0o777 == 0o600
    cert = ca.issue_from_csr((tmp_path / "me.csr").read_bytes(), _subject())
    (tmp_path / "me.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "doc.pdf").write_bytes(DOC)
    assert keytool.main(["sign", "--key", str(tmp_path / "me.key"), "--cert", str(tmp_path / "me.crt"),
                         "--password-env", "KP", str(tmp_path / "doc.pdf"), "--out", str(tmp_path / "doc.p7s")]) == 0
    capsys.readouterr()
    assert keytool.main(["verify", "--sig", str(tmp_path / "doc.p7s"), "--ca", str(tmp_path / "ca/chain.pem"),
                         str(tmp_path / "doc.pdf")]) == 0
    assert '"level": "internal"' in capsys.readouterr().out
    (tmp_path / "other.pdf").write_bytes(b"changed")
    assert keytool.main(["verify", "--sig", str(tmp_path / "doc.p7s"), "--ca", str(tmp_path / "ca/chain.pem"),
                         str(tmp_path / "other.pdf")]) == 1
    if shutil.which("openssl"):  # незалежна перевірка контейнера
        (tmp_path / "anchors.pem").write_bytes(ca.chain_pem)
        out = subprocess.run(["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-in", str(tmp_path / "doc.p7s"),
                              "-content", str(tmp_path / "doc.pdf"), "-CAfile", str(tmp_path / "anchors.pem"),
                              "-purpose", "any", "-out", "/dev/null"], capture_output=True, text=True)
        assert "Verification successful" in out.stderr, out.stderr
    b64 = base64.b64encode((tmp_path / "doc.p7s").read_bytes())
    (tmp_path / "doc.b64").write_bytes(b64)
    assert keytool.main(["verify", "--sig", str(tmp_path / "doc.b64"), "--ca", str(tmp_path / "ca/chain.pem"),
                         "--sha256", DOC_SHA]) == 0
