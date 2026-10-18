"""Результати перевірки КЕП і порти, від яких залежить верифікатор."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from cryptography import x509
from cryptography.exceptions import InvalidSignature

from sed_db.enums import SignatureFormat, SignatureValidation

from .certinfo import CertLevel


@dataclass(frozen=True)
class CertificateInfo:
    serial_hex: str
    issuer: str
    subject_cn: str
    valid_from: datetime
    valid_to: datetime
    is_qualified: bool
    fingerprint_sha256: str
    # РНОКПП лише в пам'яті під час перевірки; у БД іде тільки HMAC (signer_tax_id_hmac).
    tax_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class VerificationResult:
    status: SignatureValidation
    reasons: tuple[str, ...]
    signature_format: SignatureFormat | None = None
    certificate: CertificateInfo | None = None
    signed_at: datetime | None = None
    signed_digest_hex: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    level: CertLevel | None = None

    @property
    def ok(self) -> bool:
        return self.status is SignatureValidation.VALID

    def report(self) -> dict[str, Any]:
        """Для `signatures.validation_report` (без РНОКПП)."""
        c = self.certificate
        return {
            "status": self.status.value,
            "reasons": list(self.reasons),
            "format": self.signature_format.value if self.signature_format else None,
            "level": self.level.value if self.level else None,
            "signed_at": self.signed_at.isoformat() if self.signed_at else None,
            "checks": self.checks,
            "certificate": None if c is None else {
                "serial": c.serial_hex, "issuer": c.issuer, "subject_cn": c.subject_cn,
                "valid_from": c.valid_from.isoformat(), "valid_to": c.valid_to.isoformat(),
                "qualified": c.is_qualified, "sha256": c.fingerprint_sha256,
            },
        }


class RevocationStatus(StrEnum):
    GOOD = "good"
    REVOKED = "revoked"
    UNKNOWN = "unknown"


class RevocationChecker(Protocol):
    """OCSP/CRL КНЕДП. UNKNOWN (недоступний OCSP) — підпис не приймається."""

    async def status(self, cert: x509.Certificate, issuer: x509.Certificate,
                     at: datetime) -> RevocationStatus: ...


class TrustStore(Protocol):
    """Довірені сертифікати КНЕДП (з Довірчого списку ЦЗО)."""

    def find_issuer(self, cert: x509.Certificate) -> x509.Certificate | None: ...


class CertificateBinding(Protocol):
    """Чий це сертифікат. True — закріплений за цим користувачем, False — за іншим або не його
    РНОКПП, None — за користувачем ще не закріплено жодного ключа."""

    async def matches(self, user_id: uuid.UUID, fingerprint: str, tax_id_hmac: str | None) -> bool | None: ...


class TaxIdHasher:
    """HMAC-SHA256 РНОКПП з секретом застосунку: у БД не зберігається сам номер."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("Секрет HMAC для РНОКПП має бути щонайменше 32 байти")
        self._secret = secret

    def __call__(self, tax_id: str) -> str:
        return hmac.new(self._secret, tax_id.strip().encode(), hashlib.sha256).hexdigest()


class StaticCertificateBinding:
    """Прив'язка з конфігурації/пам'яті (тести, пілот). Продакшн — таблиця, див. README."""

    def __init__(self, mapping: dict[uuid.UUID, str] | None = None) -> None:
        self._map = dict(mapping or {})

    def bind(self, user_id: uuid.UUID, tax_id_hmac: str) -> None:
        self._map[user_id] = tax_id_hmac

    async def matches(self, user_id: uuid.UUID, fingerprint: str, tax_id_hmac: str | None) -> bool | None:
        bound = self._map.get(user_id)
        if bound is None:
            return None
        return tax_id_hmac is not None and hmac.compare_digest(bound, tax_id_hmac)


class PemTrustStore:
    def __init__(self, anchors: list[x509.Certificate]) -> None:
        self.anchors = anchors

    @classmethod
    def from_pem(cls, data: bytes) -> PemTrustStore:
        return cls(x509.load_pem_x509_certificates(data))

    def find_issuer(self, cert: x509.Certificate) -> x509.Certificate | None:
        for ca in self.anchors:
            if ca.subject != cert.issuer:
                continue
            try:
                cert.verify_directly_issued_by(ca)
            except (ValueError, TypeError, InvalidSignature):
                continue
            return ca
        return None

    def find_issuer_structural(self, cert: x509.Certificate) -> x509.Certificate | None:
        """Для сертифікатів ДСТУ: збіг імені видавця й ідентифікаторів ключа (AKI = SKI).
        Криптографічну перевірку ланцюжка робить сертифікований провайдер."""
        try:
            aki = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier
        except x509.ExtensionNotFound:
            aki = None
        for ca in self.anchors:
            if ca.subject != cert.issuer:
                continue
            try:
                ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
            except x509.ExtensionNotFound:
                ski = None
            if aki is not None and ski is not None and aki == ski:
                return ca
        return None
