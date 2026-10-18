"""Сертифікати й контейнери «як у Дії / ПриватБанку» (ДСТУ 4145, ГОСТ 34.311) для тестів.

Структура ASN.1 справжня (OID ДСТУ, РНОКПП у subjectDirectoryAttributes і serialNumber TINUA-,
QcCompliance, AKI/SKI), а математику ДСТУ імітує `FakeDstuSidecar`: у реальному розгортанні це
сервіс на бібліотеці ІІТ. Геш ГОСТ 34.311 у тестах підміняє SHA-256 — лише в межах фейку.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from asn1crypto import algos, cms, core, keys
from asn1crypto import x509 as ax

from sed_routing.kep.cades import build_detached_cades_bes, parse_detached, qc_statements_der
from sed_routing.kep.certinfo import DRFO_OID
from sed_routing.kep.external import ExternalVerification, ExternalVerifierUnavailable
from sed_routing.kep.models import RevocationStatus

DSTU_SIG = "1.2.804.2.1.1.1.1.3.1.1"
GOST_HASH = "1.2.804.2.1.1.1.1.2.1"


def _ext(oid: str, value: core.Asn1Value | bytes, critical: bool = False) -> ax.Extension:
    raw = value if isinstance(value, bytes) else value.dump()
    return ax.Extension({"extn_id": oid, "critical": critical, "extn_value": core.ParsableOctetString(raw)})


def _tlv(tag: int, content: bytes) -> bytes:
    n = len(content)
    size = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + size + content


def _dir_attrs(drfo: str) -> bytes:
    """SubjectDirectoryAttributes з РНОКПП (DRFO), як у сертифікатах українських КНЕДП."""
    attr = _tlv(0x30, core.ObjectIdentifier(DRFO_OID).dump() + _tlv(0x31, core.PrintableString(drfo).dump()))
    return _tlv(0x30, attr)


def dstu_cert(*, subject: dict, issuer: ax.Name, serial: int, ski: bytes, aki: bytes | None, ca: bool,
              drfo: str | None = None, qualified: bool = True, days: int = 365,
              not_before: datetime | None = None) -> bytes:
    nb = (not_before or datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
    exts = [_ext("2.5.29.15", ax.KeyUsage({"key_cert_sign", "crl_sign"} if ca else
                                          {"digital_signature", "non_repudiation"}), True),
            _ext("2.5.29.14", core.OctetString(ski))]
    if aki is not None:
        exts.append(_ext("2.5.29.35", ax.AuthorityKeyIdentifier({"key_identifier": aki})))
    if ca:
        exts.append(_ext("2.5.29.19", ax.BasicConstraints({"ca": True}), True))
    if qualified and not ca:
        exts.append(_ext("1.3.6.1.5.5.7.1.3", qc_statements_der()))
    if drfo:
        exts.append(_ext("2.5.29.9", _dir_attrs(drfo)))
    alg = _tlv(0x30, core.ObjectIdentifier(DSTU_SIG).dump())
    spki = keys.PublicKeyInfo.load(_tlv(0x30, alg + _tlv(0x03, b"\x00" + core.OctetString(os.urandom(32)).dump())))
    tbs = ax.TbsCertificate({
        "version": "v3", "serial_number": serial, "signature": {"algorithm": DSTU_SIG},
        "issuer": issuer, "subject": ax.Name.build(subject),
        "validity": {"not_before": ax.Time(name="utc_time", value=nb),
                     "not_after": ax.Time(name="utc_time", value=nb + timedelta(days=days))},
        "subject_public_key_info": spki, "extensions": exts})
    return ax.Certificate({"tbs_certificate": tbs, "signature_algorithm": {"algorithm": DSTU_SIG},
                           "signature_value": os.urandom(64)}).dump()


@dataclass
class DstuKnedp:
    """«КНЕДП ДІЯ» для тестів: сертифікат ЦСК і користувацькі сертифікати з РНОКПП."""

    name: str = "КНЕДП - ДІЯ (тест)"
    ski: bytes = field(default_factory=lambda: os.urandom(20))
    ca_der: bytes = b""
    revoked: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.ca_name = ax.Name.build({"country_name": "UA", "organization_name": "Тестовий ЦСК",
                                      "common_name": self.name})
        self.ca_der = dstu_cert(subject={"country_name": "UA", "organization_name": "Тестовий ЦСК",
                                         "common_name": self.name},
                                issuer=self.ca_name, serial=1, ski=self.ski, aki=self.ski, ca=True, days=3650)

    @property
    def ca_pem(self) -> bytes:
        import base64
        b = base64.encodebytes(self.ca_der).replace(b"\n", b"")
        lines = b"\n".join(b[i:i + 64] for i in range(0, len(b), 64))
        return b"-----BEGIN CERTIFICATE-----\n" + lines + b"\n-----END CERTIFICATE-----\n"

    def issue(self, cn: str, tax_id: str, *, via_serial_number: bool = True, **kw) -> bytes:  # noqa: ANN003
        subj = {"country_name": "UA", "common_name": cn, "surname": cn.split()[-1]}
        if via_serial_number:
            subj["serial_number"] = f"TINUA-{tax_id}"
        return dstu_cert(subject=subj, issuer=self.ca_name, serial=int.from_bytes(os.urandom(8), "big"),
                         ski=os.urandom(20), aki=self.ski, ca=False,
                         drfo=None if via_serial_number else tax_id, **kw)


def dstu_sign(cert_der: bytes, content: bytes, signing_time: datetime | None = None) -> bytes:
    """Контейнер CAdES-BES з OID ДСТУ 4145 / ГОСТ 34.311 (підпис-заглушка; «геш» — SHA-256)."""
    p7 = build_detached_cades_bes(cert_der=cert_der, content_sha256=hashlib.sha256(content).digest(),
                                  signing_time=(signing_time or datetime.now(timezone.utc)).replace(microsecond=0),
                                  sign=lambda d: b"FAKE-DSTU" + hashlib.sha256(d).digest(),
                                  signature_algorithm=DSTU_SIG)
    ci = cms.ContentInfo.load(p7)
    sd = ci["content"]
    sd["digest_algorithms"] = cms.DigestAlgorithms([algos.DigestAlgorithm({"algorithm": GOST_HASH})])
    sd["signer_infos"][0]["digest_algorithm"] = algos.DigestAlgorithm({"algorithm": GOST_HASH})
    return cms.ContentInfo({"content_type": "signed_data", "content": sd}).dump()


class FakeDstuSidecar:
    """Те, що робить сервіс на бібліотеці ІІТ: математика ДСТУ, ланцюжок, OCSP КНЕДП."""

    def __init__(self, knedp: DstuKnedp, *, down: bool = False) -> None:
        self.knedp, self.down, self.calls = knedp, down, 0

    async def verify_detached(self, container: bytes, data: bytes) -> ExternalVerification:
        self.calls += 1
        if self.down:
            raise ExternalVerifierUnavailable("connection refused")
        p = parse_detached(container)
        ok = p.message_digest == hashlib.sha256(data).digest() and p.signature.startswith(b"FAKE-DSTU") \
            and p.signature[9:] == hashlib.sha256(p.signed_attrs_der).digest()
        serial = format(ax.Certificate.load(p.signer_cert_der).serial_number, "X")
        return ExternalVerification(
            valid=ok, reasons=() if ok else ("Підпис ДСТУ 4145 недійсний",), signer_certificate_der=p.signer_cert_der,
            signing_time=p.signing_time, chain_valid=ax.Certificate.load(p.signer_cert_der).issuer == self.knedp.ca_name,
            revocation=RevocationStatus.REVOKED if serial in self.knedp.revoked else RevocationStatus.GOOD,
            signature_format="CAdES-BES")
