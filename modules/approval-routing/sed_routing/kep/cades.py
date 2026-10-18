"""Структура відокремленого підпису CAdES-BES (CMS SignedData, RFC 5652 + ETSI EN 319 122-1).

Тут лише ASN.1: збирання контейнера (для імітації КНЕДП) і розбір із перевіркою структури.
Криптографічна перевірка і політика — у `verifier.py`.

Українські КЕП зазвичай використовують ДСТУ 4145 і геш ГОСТ 34.311 / ДСТУ 7564 («Купина»):
ці алгоритми розпізнаються, але перевірка потребує сертифікованого криптопровайдера
(ІІТ «Користувач ЦСК» / бібліотеки Дія.Підпис) — див. README, «Реальний КЕП».
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from asn1crypto import algos, cms, core, tsp
from asn1crypto import x509 as ax509

# Алгоритми ДСТУ, які лише розпізнаємо (OID з реєстру 1.2.804.2.1.1.1).
DSTU_DIGESTS = {
    "1.2.804.2.1.1.1.1.2.1": "ГОСТ 34.311-95",
    "1.2.804.2.1.1.1.1.2.2.1": "ДСТУ 7564:2014 (256)",
    "1.2.804.2.1.1.1.1.2.2.2": "ДСТУ 7564:2014 (384)",
    "1.2.804.2.1.1.1.1.2.2.3": "ДСТУ 7564:2014 (512)",
}
DSTU_SIGNATURES = {
    "1.2.804.2.1.1.1.1.3.1.1": "ДСТУ 4145-2002 (поліноміальний базис)",
    "1.2.804.2.1.1.1.1.3.1.2": "ДСТУ 4145-2002 (оптимальний нормальний базис)",
}
QC_STATEMENTS_OID = "1.3.6.1.5.5.7.1.3"
QC_COMPLIANCE_OID = "0.4.0.1862.1.1"   # ETSI: кваліфікований сертифікат


class _QcStatement(core.Sequence):
    _fields = [("statement_id", core.ObjectIdentifier), ("statement_info", core.Any, {"optional": True})]


class QcStatements(core.SequenceOf):
    _child_spec = _QcStatement


class StructureError(ValueError):
    """Контейнер не є коректним відокремленим CAdES."""


@dataclass(frozen=True)
class ParsedSignature:
    signer_cert_der: bytes
    digest_algorithm: str          # dotted OID або ім'я asn1crypto (sha256 …)
    signature_algorithm: str
    message_digest: bytes
    signing_time: datetime | None
    signed_attrs_der: bytes        # саме ці байти підписані (SET OF, DER)
    signature: bytes
    signing_cert_v2_hash: bytes | None
    signing_cert_v2_alg: str | None
    has_timestamp: bool
    has_revocation_values: bool


def build_detached_cades_bes(
    *, cert_der: bytes, content_sha256: bytes, signing_time: datetime,
    sign: Callable[[bytes], bytes],
    signature_algorithm: str = "sha256_ecdsa",
) -> bytes:
    """Збирає CAdES-BES: content-type, signing-time, message-digest, signing-certificate-v2."""
    cert = ax509.Certificate.load(cert_der)
    ess_cert = tsp.ESSCertIDv2({
        "hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
        "cert_hash": hashlib.sha256(cert_der).digest(),
        "issuer_serial": tsp.IssuerSerial({
            "issuer": ax509.GeneralNames([ax509.GeneralName(name="directory_name", value=cert.issuer)]),
            "serial_number": cert.serial_number,
        }),
    })
    signed_attrs = cms.CMSAttributes([
        cms.CMSAttribute({"type": "content_type", "values": ["data"]}),
        cms.CMSAttribute({"type": "signing_time", "values": [cms.Time(name="utc_time", value=signing_time)]}),
        cms.CMSAttribute({"type": "message_digest", "values": [content_sha256]}),
        cms.CMSAttribute({"type": "signing_certificate_v2",
                          "values": [tsp.SigningCertificateV2({"certs": [ess_cert]})]}),
    ])
    signature = sign(signed_attrs.dump())
    signer_info = cms.SignerInfo({
        "version": "v1",
        "sid": cms.SignerIdentifier(name="issuer_and_serial_number", value=cms.IssuerAndSerialNumber({
            "issuer": cert.issuer, "serial_number": cert.serial_number})),
        "digest_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
        "signed_attrs": signed_attrs,
        "signature_algorithm": algos.SignedDigestAlgorithm({"algorithm": signature_algorithm}),
        "signature": signature,
    })
    signed_data = cms.SignedData({
        "version": "v1",
        "digest_algorithms": [algos.DigestAlgorithm({"algorithm": "sha256"})],
        "encap_content_info": {"content_type": "data"},
        "certificates": [cert],
        "signer_infos": [signer_info],
    })
    return cms.ContentInfo({"content_type": "signed_data", "content": signed_data}).dump()


def _attr(attrs: cms.CMSAttributes, name: str) -> core.Asn1Value | None:
    found = [a for a in attrs if a["type"].native == name]
    if len(found) > 1:
        raise StructureError(f"Атрибут {name} повторюється")
    if not found:
        return None
    values = found[0]["values"]
    if len(values) != 1:
        raise StructureError(f"Атрибут {name} має містити одне значення")
    return values[0]


def _alg(value: core.ObjectIdentifier) -> str:
    try:
        return value.native
    except ValueError:  # невідомий asn1crypto OID
        return value.dotted


def parse_detached(container: bytes) -> ParsedSignature:
    try:
        ci = cms.ContentInfo.load(container, strict=True)
        if ci["content_type"].native != "signed_data":
            raise StructureError("Очікується CMS SignedData")
        sd: cms.SignedData = ci["content"]
        if sd["encap_content_info"]["content"].native is not None:
            raise StructureError("Очікується відокремлений (detached) підпис без вкладеного документа")
        if sd["encap_content_info"]["content_type"].native != "data":
            raise StructureError("Підписаний вміст має бути типу id-data")
        infos = sd["signer_infos"]
        if len(infos) != 1:
            raise StructureError("Контейнер має містити рівно один підпис (кожен учасник підписує окремо)")
        si: cms.SignerInfo = infos[0]
        certs = [c.chosen for c in (sd["certificates"] or []) if c.name == "certificate"]
        sid = si["sid"]
        if sid.name == "issuer_and_serial_number":
            want = sid.chosen
            match = [c for c in certs if c.issuer == want["issuer"] and c.serial_number == want["serial_number"].native]
        else:
            match = [c for c in certs if c.key_identifier == sid.chosen.native]
        if len(match) != 1:
            raise StructureError("Сертифікат підписувача відсутній у контейнері")
        attrs = si["signed_attrs"]
        if not attrs:
            raise StructureError("Немає підписаних атрибутів (signed attributes)")
        ctype = _attr(attrs, "content_type")
        if ctype is None or ctype.native != "data":
            raise StructureError("Атрибут content-type має бути id-data")
        md = _attr(attrs, "message_digest")
        if md is None:
            raise StructureError("Немає атрибута message-digest")
        st = _attr(attrs, "signing_time")
        scv2 = _attr(attrs, "signing_certificate_v2")
        cert_hash, cert_alg = None, None
        if scv2 is not None:
            first = scv2["certs"][0]
            cert_hash, cert_alg = first["cert_hash"].native, first["hash_algorithm"]["algorithm"].native
        unsigned = si["unsigned_attrs"]
        utypes = {a["type"].dotted for a in unsigned} if unsigned else set()
        return ParsedSignature(
            signer_cert_der=match[0].dump(),
            digest_algorithm=_alg(si["digest_algorithm"]["algorithm"]),
            signature_algorithm=_alg(si["signature_algorithm"]["algorithm"]),
            message_digest=md.native,
            signing_time=st.native if st is not None else None,
            signed_attrs_der=attrs.untag().dump(),
            signature=si["signature"].native,
            signing_cert_v2_hash=cert_hash,
            signing_cert_v2_alg=cert_alg,
            has_timestamp="1.2.840.113549.1.9.16.2.14" in utypes,
            has_revocation_values="1.2.840.113549.1.9.16.2.24" in utypes,
        )
    except StructureError:
        raise
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise StructureError(f"Некоректна ASN.1-структура контейнера: {exc}") from exc


def is_qualified(cert_der: bytes) -> bool:
    cert = ax509.Certificate.load(cert_der)
    for ext in cert["tbs_certificate"]["extensions"] or []:
        if ext["extn_id"].dotted == QC_STATEMENTS_OID:
            try:
                stmts = QcStatements.load(ext["extn_value"].contents)
                return any(s["statement_id"].dotted == QC_COMPLIANCE_OID for s in stmts)
            except ValueError:
                return False
    return False


def qc_statements_der() -> bytes:
    """Значення розширення qcStatements з QcCompliance (для імітації кваліфікованого сертифіката)."""
    return QcStatements([{"statement_id": QC_COMPLIANCE_OID}]).dump()
