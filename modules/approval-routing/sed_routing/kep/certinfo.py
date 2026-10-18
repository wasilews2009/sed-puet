"""Розбір сертифіката будь-якого КНЕДП, зокрема з ключами ДСТУ 4145.

Працює через asn1crypto, тому читає й ті сертифікати, чий відкритий ключ `cryptography` не
підтримує (ДСТУ 4145). Криптографічну перевірку ланцюжка таких сертифікатів виконує
сертифікований провайдер (див. `external.py`); тут — структура, строки, ознаки кваліфікованості,
РНОКПП/ЄДРПОУ, адреси OCSP/CRL і рівень (кваліфікований / внутрішній СЕД / імітація).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from asn1crypto import core
from asn1crypto import x509 as ax509

from .cades import DSTU_SIGNATURES, QC_COMPLIANCE_OID, QC_STATEMENTS_OID, QcStatements

# OID з реєстру 1.2.804.2.1.1.1.11.1.4 (атрибути суб'єкта в українських сертифікатах)
DRFO_OID = "1.2.804.2.1.1.1.11.1.4.1.1"    # РНОКПП
EDRPOU_OID = "1.2.804.2.1.1.1.11.1.4.2.1"  # код ЄДРПОУ
DSTU4145_KEY_OIDS = {"1.2.804.2.1.1.1.1.3.1.1", "1.2.804.2.1.1.1.1.3.1.2"}
UID_OID = "0.9.2342.19200300.100.1.1"
TAX_ID_PREFIX = "TINUA-"
MOCK_POLICY_OID = "1.3.6.1.4.1.59999.1.1"      # імітаційний КНЕДП (тести, dev)
INTERNAL_POLICY_OID = "1.3.6.1.4.1.59999.2.1"  # внутрішній ЦСК «АІ-СЕД ПУЕТ»
DOCUMENT_SIGNING_EKU = "1.3.6.1.5.5.7.3.36"    # id-kp-documentSigning (RFC 9336)


class CertLevel(StrEnum):
    QUALIFIED = "qualified"   # кваліфікований сертифікат КНЕДП (Дія, ПриватБанк, ДПС…)
    INTERNAL = "internal"     # внутрішній ключ СЕД: діє лише в межах «АІ-СЕД ПУЕТ»
    MOCK = "mock"             # імітація для тестів
    OTHER = "other"           # некваліфікований сторонній — для погодження не приймається


LEVEL_LABELS_UK = {
    CertLevel.QUALIFIED: "Кваліфікований (КЕП)",
    CertLevel.INTERNAL: "Внутрішній ключ АІ-СЕД ПУЕТ (не КЕП)",
    CertLevel.MOCK: "Імітація (тестовий КНЕДП)",
    CertLevel.OTHER: "Некваліфікований сертифікат",
}

_KEY_ALGO_UK = {"rsa": "RSA", "ec": "ECDSA", "dsa": "DSA", "ed25519": "Ed25519", "ed448": "Ed448"}


class CertificateParseError(ValueError):
    pass


@dataclass(frozen=True)
class CertReport:
    subject_cn: str
    subject: str
    issuer: str
    serial_hex: str
    valid_from: datetime
    valid_to: datetime
    key_algorithm: str
    is_dstu: bool
    is_ca: bool
    is_qualified: bool
    level: CertLevel
    key_usage: tuple[str, ...]
    fingerprint_sha256: str
    subject_key_id: str | None
    authority_key_id: str | None
    ocsp_urls: tuple[str, ...] = ()
    crl_urls: tuple[str, ...] = ()
    edrpou: str | None = None
    sed_user_id: uuid.UUID | None = None
    tax_id: str | None = field(default=None, repr=False)   # лише в пам'яті
    der: bytes = field(default=b"", repr=False, compare=False)

    def validity_problem(self, at: datetime | None = None) -> str | None:
        at = at or datetime.now(timezone.utc)
        if at < self.valid_from:
            return "Сертифікат ще не набув чинності"
        if at > self.valid_to:
            return "Строк дії сертифіката минув"
        return None

    def public_dict(self) -> dict[str, object]:
        """Для API/звітів: РНОКПП маскується (ЗУ «Про захист персональних даних»)."""
        subject = self.subject.replace(self.tax_id, mask_tax_id(self.tax_id) or "") if self.tax_id else self.subject
        return {
            "subject_cn": self.subject_cn, "subject": subject, "issuer": self.issuer,
            "serial": self.serial_hex, "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(), "key_algorithm": self.key_algorithm,
            "is_dstu": self.is_dstu, "is_qualified": self.is_qualified, "level": self.level.value,
            "level_uk": LEVEL_LABELS_UK[self.level], "key_usage": list(self.key_usage),
            "sha256": self.fingerprint_sha256, "ocsp": list(self.ocsp_urls), "crl": list(self.crl_urls),
            "edrpou": self.edrpou, "tax_id_masked": mask_tax_id(self.tax_id),
            "sed_user_id": str(self.sed_user_id) if self.sed_user_id else None,
        }


def mask_tax_id(tax_id: str | None) -> str | None:
    return f"******{tax_id[-4:]}" if tax_id and len(tax_id) >= 4 else None


def _name_attr(name: ax509.Name, oid: str) -> list[str]:
    out = []
    for rdn in name.chosen:
        for atv in rdn:
            if atv["type"].dotted == oid:
                v = atv["value"].native
                out.append(v if isinstance(v, str) else str(v))
    return out


def _policies(cert: ax509.Certificate) -> set[str]:
    pol = cert.certificate_policies_value
    return {p["policy_identifier"].dotted for p in pol} if pol else set()


def _any_text(value: core.Any) -> str:
    try:
        parsed = value.parse()
        v = parsed.native
    except (ValueError, TypeError):
        v = value.contents
    return v.decode(errors="replace") if isinstance(v, bytes) else str(v)


def _subject_dir_attr_text(cert: ax509.Certificate, oid: str) -> str | None:
    for ext in cert["tbs_certificate"]["extensions"] or []:
        if ext["extn_id"].dotted != "2.5.29.9":
            continue
        try:
            seq = core.Sequence.load(ext["extn_value"].contents)
            for i in range(len(seq)):
                attr = core.Sequence.load(seq[i].dump())
                attr_oid = core.ObjectIdentifier.load(attr[0].dump()).dotted
                if attr_oid == oid:
                    values = core.SetOf.load(attr[1].dump(), spec=core.Any)
                    return _any_text(values[0]) if len(values) else None
        except (ValueError, TypeError, IndexError):
            return None
    return None


def analyze_certificate(der: bytes) -> CertReport:
    try:
        cert = ax509.Certificate.load(der, strict=True)
        tbs = cert["tbs_certificate"]
        # OID ключа читаємо без розбору самого ключа: asn1crypto не знає ДСТУ 4145 і падає на ньому.
        spki_generic = core.Sequence.load(tbs["subject_public_key_info"].dump())
        algo_oid = core.ObjectIdentifier.load(core.Sequence.load(spki_generic[0].dump())[0].dump()).dotted
        is_dstu = algo_oid in DSTU4145_KEY_OIDS
        if is_dstu:
            key_alg = "ДСТУ 4145-2002"
        else:
            spki = tbs["subject_public_key_info"]
            name = _KEY_ALGO_UK.get(spki.algorithm, algo_oid)
            try:
                bits = spki.bit_size
            except (ValueError, TypeError):
                bits = None
            curve = ""
            if spki.algorithm == "ec":
                try:
                    curve = f" {spki.curve[1]}"
                except (ValueError, TypeError):
                    curve = ""
            key_alg = f"{name}{curve}" + (f" ({bits} біт)" if bits and spki.algorithm == "rsa" else "")
        qualified = False
        for ext in tbs["extensions"] or []:
            if ext["extn_id"].dotted == QC_STATEMENTS_OID:
                try:
                    qualified = any(s["statement_id"].dotted == QC_COMPLIANCE_OID
                                    for s in QcStatements.load(ext["extn_value"].contents))
                except ValueError:
                    qualified = False
        policies = _policies(cert)
        if MOCK_POLICY_OID in policies:
            level = CertLevel.MOCK
        elif INTERNAL_POLICY_OID in policies:
            level = CertLevel.INTERNAL
        elif qualified:
            level = CertLevel.QUALIFIED
        else:
            level = CertLevel.OTHER
        tax_id = None
        for sn in _name_attr(cert.subject, "2.5.4.5"):
            if sn.startswith(TAX_ID_PREFIX) and sn[len(TAX_ID_PREFIX):].isdigit():
                tax_id = sn[len(TAX_ID_PREFIX):]
        if tax_id is None:
            drfo = _subject_dir_attr_text(cert, DRFO_OID)
            tax_id = drfo if drfo and drfo.isdigit() else None
        edrpou = _subject_dir_attr_text(cert, EDRPOU_OID)
        uid = _name_attr(cert.subject, UID_OID)
        try:
            sed_user = uuid.UUID(uid[0]) if uid else None
        except ValueError:
            sed_user = None
        ku = cert.key_usage_value
        aia = cert.authority_information_access_value
        ocsp = tuple(d["access_location"].native for d in aia or []
                     if d["access_method"].native == "ocsp")
        crl = tuple(u for u in (cert.crl_distribution_points or []) for u in [_crl_url(u)] if u)
        cn = _name_attr(cert.subject, "2.5.4.3")
        return CertReport(
            subject_cn=cn[0] if cn else cert.subject.human_friendly, subject=cert.subject.human_friendly,
            issuer=cert.issuer.human_friendly, serial_hex=format(cert.serial_number, "X"),
            valid_from=tbs["validity"]["not_before"].native, valid_to=tbs["validity"]["not_after"].native,
            key_algorithm=key_alg, is_dstu=is_dstu, is_ca=bool(cert.ca), is_qualified=qualified, level=level,
            key_usage=tuple(sorted(ku.native)) if ku else (),
            fingerprint_sha256=hashlib.sha256(der).hexdigest(),
            subject_key_id=cert.key_identifier.hex() if cert.key_identifier else None,
            authority_key_id=cert.authority_key_identifier.hex() if cert.authority_key_identifier else None,
            ocsp_urls=ocsp, crl_urls=crl, edrpou=edrpou if edrpou and edrpou.isdigit() else None,
            sed_user_id=sed_user, tax_id=tax_id, der=der)
    except (ValueError, TypeError, KeyError) as exc:
        raise CertificateParseError(f"Некоректний сертифікат X.509: {exc}") from exc


def _crl_url(dp) -> str | None:  # noqa: ANN001
    try:
        for gn in dp["distribution_point"].chosen:
            if gn.name == "uniform_resource_identifier":
                return gn.native
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return None


def is_dstu_signature_algorithm(oid: str) -> bool:
    return oid in DSTU_SIGNATURES


def load_certificate_bytes(data: bytes) -> bytes:
    """DER з DER/PEM/base64."""
    import base64
    import re

    data = data.strip()
    if data.startswith(b"-----BEGIN"):
        m = re.search(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", data, re.S)
        if not m:
            raise CertificateParseError("PEM не містить сертифіката")
        return base64.b64decode(b"".join(m.group(1).split()))
    if data[:1] == b"\x30":
        return data
    try:
        return base64.b64decode(data, validate=True)
    except ValueError as exc:
        raise CertificateParseError("Очікується сертифікат у DER, PEM або base64") from exc
