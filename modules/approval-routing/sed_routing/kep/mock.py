"""Імітаційний КНЕДП для розробки, тестів і демонстрації без справжніх ключів.

Видає сертифікати ECDSA P-256 зі структурою як у кваліфікованих (QcCompliance, keyUsage
nonRepudiation, serialNumber «TINUA-<РНОКПП>») і підписує геш документа справжнім
відокремленим CAdES-BES, тож верифікатор перевіряє його так само, як і реальний. Сертифікати
позначені політикою MOCK_POLICY_OID: верифікатор відкидає їх, якщо `allow_mock=False`
(значення за замовчуванням у продакшні).

Імітація не замінює КЕП: юридичну силу має лише підпис, накладений ключем, виданим
кваліфікованим надавачем (КНЕДП) з Довірчого списку.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .cades import QC_STATEMENTS_OID, build_detached_cades_bes, qc_statements_der
from .models import RevocationStatus
from .certinfo import MOCK_POLICY_OID, TAX_ID_PREFIX


@dataclass
class MockKey:
    """Особистий ключ користувача (у справжньому КЕП — на токені/у хмарі, сервер його не бачить)."""

    private_key: ec.EllipticCurvePrivateKey
    certificate: x509.Certificate

    @property
    def cert_der(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.DER)

    def sign(self, content_sha256_hex: str, signing_time: datetime | None = None) -> bytes:
        return build_detached_cades_bes(
            cert_der=self.cert_der,
            content_sha256=bytes.fromhex(content_sha256_hex),
            signing_time=(signing_time or datetime.now(timezone.utc)).replace(microsecond=0),
            sign=lambda data: self.private_key.sign(data, ec.ECDSA(hashes.SHA256())),
        )


@dataclass
class MockKnedp:
    name: str = "Тестовий КНЕДП ПУЕТ (MOCK)"
    revoked_serials: set[int] = field(default_factory=set)
    ocsp_available: bool = True

    def __post_init__(self) -> None:
        self._ca_key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "UA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MOCK"),
            x509.NameAttribute(NameOID.COMMON_NAME, self.name),
        ])
        now = datetime.now(timezone.utc)
        self.ca_certificate = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(self._ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=365)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self._ca_key.public_key()), critical=False)
            .add_extension(x509.CertificatePolicies([x509.PolicyInformation(
                x509.ObjectIdentifier(MOCK_POLICY_OID), None)]), critical=False)
            .sign(self._ca_key, hashes.SHA256())
        )

    @property
    def ca_pem(self) -> bytes:
        return self.ca_certificate.public_bytes(serialization.Encoding.PEM)

    def issue(
        self, full_name: str, tax_id: str, *, valid_from: datetime | None = None,
        valid_to: datetime | None = None, qualified: bool = True, key_usage_signing: bool = True,
    ) -> MockKey:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COUNTRY_NAME, "UA"),
                x509.NameAttribute(NameOID.COMMON_NAME, full_name),
                x509.NameAttribute(NameOID.SERIAL_NUMBER, f"{TAX_ID_PREFIX}{tax_id}"),
            ]))
            .issuer_name(self.ca_certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(valid_from or now - timedelta(days=30))
            .not_valid_after(valid_to or now + timedelta(days=335))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(key_usage_signing, key_usage_signing, False, False, False,
                                         False, False, False, False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_key.public_key()),
                           critical=False)
            .add_extension(x509.CertificatePolicies([x509.PolicyInformation(
                x509.ObjectIdentifier(MOCK_POLICY_OID), None)]), critical=False)
        )
        if qualified:
            builder = builder.add_extension(x509.UnrecognizedExtension(
                x509.ObjectIdentifier(QC_STATEMENTS_OID), qc_statements_der()), critical=False)
        return MockKey(key, builder.sign(self._ca_key, hashes.SHA256()))

    def revoke(self, key: MockKey) -> None:
        self.revoked_serials.add(key.certificate.serial_number)

    # Реалізація порту RevocationChecker (імітація OCSP-відповідача).
    async def status(self, cert: x509.Certificate, issuer: x509.Certificate, at: datetime) -> RevocationStatus:
        if not self.ocsp_available:
            return RevocationStatus.UNKNOWN
        return RevocationStatus.REVOKED if cert.serial_number in self.revoked_serials else RevocationStatus.GOOD

