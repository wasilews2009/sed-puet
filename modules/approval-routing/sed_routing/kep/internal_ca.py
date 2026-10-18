"""Внутрішній ЦСК «АІ-СЕД ПУЕТ»: ключі для підпису документів лише в межах СЕД.

Це НЕ кваліфікований електронний підпис і не довірча послуга для третіх осіб. Сертифікати
позначені політикою INTERNAL_POLICY_OID і приймаються лише на тих кроках маршруту, де шаблон
явно дозволяє рівень `internal` (кроки `signing` завжди вимагають КЕП). Юридичне значення
внутрішнього підпису визначає наказ ректора / положення про електронний документообіг ПУЕТ.

Ієрархія: кореневий ЦСК (ключ зберігається офлайн) → видавальний ЦСК (ключ на сервері, у
Vault/HSM, зашифрований паролем) → сертифікат користувача (ECDSA P-256, 1 рік).
Два способи отримати ключ:
* CSR — ключ генерується на комп'ютері користувача (`keytool new-key`), сервер бачить лише
  відкритий ключ (рекомендовано);
* файл-ключ PKCS#12 — сервер генерує ключ, одразу шифрує паролем користувача (PBES2 AES-256,
  PBKDF2-SHA256) і віддає один раз; особистий ключ на сервері не зберігається.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from .certinfo import DOCUMENT_SIGNING_EKU, INTERNAL_POLICY_OID

ORG_NAME = "Полтавський університет економіки і торгівлі"
POLICY_NOTICE = "Внутрішній ключ АІ-СЕД ПУЕТ. Діє лише в межах СЕД ПУЕТ. Не є КЕП."
KEY_FILE_SUFFIX = ".puet-sed.p12"


class EnrollmentError(ValueError):
    pass


@dataclass(frozen=True)
class InternalSubject:
    user_id: uuid.UUID
    full_name: str               # «Ім'я ПРІЗВИЩЕ»
    email: str | None = None
    org_unit: str | None = None


def check_password_strength(password: str, subject: InternalSubject) -> list[str]:
    """Пароль до файлу-ключа: ≥ 12 символів, 3 класи, без імені й пошти."""
    problems = []
    if len(password) < 12:
        problems.append("щонайменше 12 символів")
    classes = sum(bool(re.search(p, password)) for p in (r"[a-zа-яіїєґ]", r"[A-ZА-ЯІЇЄҐ]", r"\d", r"[^\w\s]"))
    if classes < 3:
        problems.append("щонайменше три види символів: малі, великі літери, цифри, спецсимволи")
    low = password.casefold()
    parts = [p for p in re.split(r"[\s@.'’-]+", f"{subject.full_name} {subject.email or ''}".casefold()) if len(p) >= 4]
    if any(p in low for p in parts):
        problems.append("без імені, прізвища чи пошти")
    return problems


def _name(attrs: list[tuple[x509.ObjectIdentifier, str]]) -> x509.Name:
    return x509.Name([x509.NameAttribute(oid, v[:64]) for oid, v in attrs if v])


def _policies() -> x509.CertificatePolicies:
    return x509.CertificatePolicies([x509.PolicyInformation(
        x509.ObjectIdentifier(INTERNAL_POLICY_OID), [x509.UserNotice(None, POLICY_NOTICE)])])


@dataclass
class CaBundle:
    root_key: ec.EllipticCurvePrivateKey | None
    root_cert: x509.Certificate
    issuing_key: ec.EllipticCurvePrivateKey
    issuing_cert: x509.Certificate


def bootstrap_ca(now: datetime | None = None, *, org: str = ORG_NAME) -> CaBundle:
    """Церемонія створення ЦСК (CLI `keytool ca-init`). Кореневий ключ після цього — офлайн."""
    now = now or datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP384R1())
    root_name = _name([(NameOID.COUNTRY_NAME, "UA"), (NameOID.ORGANIZATION_NAME, org),
                       (NameOID.COMMON_NAME, "ПУЕТ АІ-СЕД Кореневий ЦСК")])
    root = (x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
            .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
            .add_extension(_policies(), critical=False)
            .sign(root_key, hashes.SHA384()))
    issuing_key = ec.generate_private_key(ec.SECP256R1())
    issuing = (x509.CertificateBuilder()
               .subject_name(_name([(NameOID.COUNTRY_NAME, "UA"), (NameOID.ORGANIZATION_NAME, org),
                                    (NameOID.COMMON_NAME, "ПУЕТ АІ-СЕД Видавальний ЦСК")]))
               .issuer_name(root_name).public_key(issuing_key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now).not_valid_after(now + timedelta(days=1825))
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(issuing_key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
               .add_extension(_policies(), critical=False)
               .sign(root_key, hashes.SHA384()))
    return CaBundle(root_key, root, issuing_key, issuing)


class InternalCa:
    def __init__(self, issuing_key: ec.EllipticCurvePrivateKey, issuing_cert: x509.Certificate,
                 root_cert: x509.Certificate, *, cert_validity_days: int = 365,
                 crl_url: str | None = None, ocsp_url: str | None = None, org: str = ORG_NAME) -> None:
        if not isinstance(issuing_key, ec.EllipticCurvePrivateKey):
            raise ValueError("Ключ видавального ЦСК має бути ECDSA")
        self.key, self.cert, self.root = issuing_key, issuing_cert, root_cert
        self.validity = timedelta(days=cert_validity_days)
        self.crl_url, self.ocsp_url, self.org = crl_url, ocsp_url, org

    @classmethod
    def from_pem(cls, issuing_key_pem: bytes, key_password: bytes | None, chain_pem: bytes, **kw) -> InternalCa:  # noqa: ANN003
        key = serialization.load_pem_private_key(issuing_key_pem, key_password)
        certs = x509.load_pem_x509_certificates(chain_pem)
        issuing = next(c for c in certs if c.public_key().public_numbers() == key.public_key().public_numbers())
        root = next(c for c in certs if c.subject == c.issuer)
        return cls(key, issuing, root, **kw)  # type: ignore[arg-type]

    @property
    def chain_pem(self) -> bytes:
        return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in (self.cert, self.root))

    def issue(self, subject: InternalSubject, public_key, now: datetime | None = None) -> x509.Certificate:  # noqa: ANN001
        now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            if public_key.curve.name not in {"secp256r1", "secp384r1"}:
                raise EnrollmentError("Дозволені криві: P-256, P-384")
        elif isinstance(public_key, rsa.RSAPublicKey):
            if public_key.key_size < 3072:
                raise EnrollmentError("RSA-ключ має бути не коротшим за 3072 біти")
        else:
            raise EnrollmentError("Дозволені ключі ECDSA або RSA")
        not_after = min(now + self.validity, self.cert.not_valid_after_utc)
        b = (x509.CertificateBuilder()
             .subject_name(_name([(NameOID.COUNTRY_NAME, "UA"), (NameOID.ORGANIZATION_NAME, self.org),
                                  (NameOID.ORGANIZATIONAL_UNIT_NAME, subject.org_unit or ""),
                                  (NameOID.COMMON_NAME, subject.full_name),
                                  (NameOID.USER_ID, str(subject.user_id))]))
             .issuer_name(self.cert.subject).public_key(public_key)
             .serial_number(x509.random_serial_number())
             .not_valid_before(now - timedelta(minutes=5)).not_valid_after(not_after)
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .add_extension(x509.KeyUsage(True, True, False, False, False, False, False, False, False), critical=True)
             .add_extension(x509.ExtendedKeyUsage([x509.ObjectIdentifier(DOCUMENT_SIGNING_EKU)]), critical=False)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
             .add_extension(_policies(), critical=False))
        if subject.email:
            b = b.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(subject.email)]), critical=False)
        if self.crl_url:
            b = b.add_extension(x509.CRLDistributionPoints([x509.DistributionPoint(
                [x509.UniformResourceIdentifier(self.crl_url)], None, None, None)]), critical=False)
        if self.ocsp_url:
            b = b.add_extension(x509.AuthorityInformationAccess([x509.AccessDescription(
                x509.oid.AuthorityInformationAccessOID.OCSP, x509.UniformResourceIdentifier(self.ocsp_url))]),
                critical=False)
        return b.sign(self.key, hashes.SHA256())

    def issue_from_csr(self, csr_pem: bytes, subject: InternalSubject, now: datetime | None = None) -> x509.Certificate:
        try:
            csr = x509.load_pem_x509_csr(csr_pem)
        except ValueError as exc:
            raise EnrollmentError("Запит на сертифікат (CSR) не читається") from exc
        if not csr.is_signature_valid:
            raise EnrollmentError("Підпис CSR недійсний: запит створено не власником ключа")
        # Суб'єкт із CSR ігнорується: ім'я і UID беруться з облікового запису СЕД.
        return self.issue(subject, csr.public_key(), now)

    def generate_key_file(self, subject: InternalSubject, password: str,
                          now: datetime | None = None) -> tuple[bytes, x509.Certificate]:
        problems = check_password_strength(password, subject)
        if problems:
            raise EnrollmentError("Слабкий пароль: " + "; ".join(problems))
        key = ec.generate_private_key(ec.SECP256R1())
        cert = self.issue(subject, key.public_key(), now)
        enc = (serialization.PrivateFormat.PKCS12.encryption_builder()
               .kdf_rounds(600_000)
               .key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC)
               .hmac_hash(hashes.SHA256())
               .build(password.encode()))
        blob = pkcs12.serialize_key_and_certificates(
            f"PUET-SED {subject.full_name}".encode(), key, cert, [self.cert, self.root], enc)
        return blob, cert

    def crl(self, revoked: list[tuple[int, datetime, x509.ReasonFlags | None]],
            now: datetime | None = None, next_update: timedelta = timedelta(hours=24)) -> bytes:
        now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        b = (x509.CertificateRevocationListBuilder().issuer_name(self.cert.subject)
             .last_update(now).next_update(now + next_update)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False))
        for serial, at, reason in revoked:
            rb = x509.RevokedCertificateBuilder().serial_number(serial).revocation_date(at)
            if reason is not None:
                rb = rb.add_extension(x509.CRLReason(reason), critical=False)
            b = b.add_revoked_certificate(rb.build())
        return b.sign(self.key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)
