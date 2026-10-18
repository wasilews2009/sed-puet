"""Перевірка відокремленого підпису перед прийняттям візи або закріпленням ключа.

Три шляхи, одна політика:
* ECDSA/RSA (імітаційний КНЕДП, сучасні КНЕДП) — криптографія тут;
* ДСТУ 4145 (файлові ключі Дії, ПриватБанку тощо) — криптографію робить сертифікований
  провайдер (`external.py`), решту політики — тут;
* внутрішній ключ «АІ-СЕД ПУЕТ» — ланцюжок до внутрішнього ЦСК і статус із реєстру видачі.

Порядок перевірок (перша фатальна зупиняє решту):
 1. розмір і структура CAdES (detached, один підписувач, обов'язкові атрибути);
 2. message-digest = геш версії документа на маршруті (для ДСТУ — перевіряє провайдер над вмістом);
 3. signing-certificate-v2 збігається з сертифікатом у контейнері;
 4. математика підпису;
 5. сертифікат видано довіреним КНЕДП / внутрішнім ЦСК; кваліфікований (для КНЕДП); keyUsage;
 6. строк дії; час підпису не з майбутнього і не раніше, ніж документ надійшов учаснику;
 7. відкликання: OCSP/CRL КНЕДП або реєстр внутрішніх сертифікатів (невідомо = не приймаємо);
 8. сертифікат закріплений саме за учасником кроку.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from sed_db.enums import SignatureFormat, SignatureValidation

from .cades import DSTU_DIGESTS, DSTU_SIGNATURES, ParsedSignature, StructureError, parse_detached
from .certinfo import INTERNAL_POLICY_OID, MOCK_POLICY_OID, CertLevel, CertReport, analyze_certificate
from .external import DstuVerifier, ExternalVerifierUnavailable
from .models import (
    CertificateBinding,
    CertificateInfo,
    PemTrustStore,
    RevocationChecker,
    RevocationStatus,
    TaxIdHasher,
    VerificationResult,
)
from .registry import InternalRevocation

_SIG_HASH = {"sha256_ecdsa": hashes.SHA256(), "sha384_ecdsa": hashes.SHA384(),
             "sha256_rsa": hashes.SHA256(), "sha384_rsa": hashes.SHA384()}

ContentLoader = Callable[[], Awaitable[bytes]]
Fail = Callable[..., VerificationResult]


def _has_policy(cert: x509.Certificate, oid: str) -> bool:
    try:
        policies = cert.extensions.get_extension_for_class(x509.CertificatePolicies).value
    except x509.ExtensionNotFound:
        return False
    return any(p.policy_identifier.dotted_string == oid for p in policies)


def is_mock_certificate(cert: x509.Certificate) -> bool:
    return _has_policy(cert, MOCK_POLICY_OID)


@dataclass(frozen=True)
class InternalTrust:
    """Внутрішній ЦСК: його сертифікат(и) і статуси виданих сертифікатів."""

    anchors: PemTrustStore
    revocation: InternalRevocation


class CadesVerifier:
    def __init__(
        self,
        trust: PemTrustStore,
        revocation: RevocationChecker,
        binding: CertificateBinding,
        tax_id_hasher: TaxIdHasher,
        *,
        internal: InternalTrust | None = None,
        dstu: DstuVerifier | None = None,
        allow_mock: bool = False,
        require_qualified: bool = True,
        max_container_bytes: int = 256 * 1024,
        clock_skew: timedelta = timedelta(minutes=5),
    ) -> None:
        self.trust, self.revocation, self.binding = trust, revocation, binding
        self.hasher, self.internal, self.dstu = tax_id_hasher, internal, dstu
        self.allow_mock, self.require_qualified = allow_mock, require_qualified
        self.max_container_bytes, self.clock_skew = max_container_bytes, clock_skew

    async def verify(
        self, container: bytes, *, expected_sha256: str, signer_user_id: uuid.UUID | None,
        not_before: datetime | None, now: datetime | None = None, content: ContentLoader | None = None,
    ) -> VerificationResult:
        """`signer_user_id=None` — режим реєстрації ключа: власника перевіряє виклик, а не реєстр."""
        now = now or datetime.now(timezone.utc)
        checks: dict[str, object] = {}

        def fail(status: SignatureValidation, reason: str, **kw: object) -> VerificationResult:
            return VerificationResult(status=status, reasons=(reason,), checks=checks, **kw)  # type: ignore[arg-type]

        if not container or len(container) > self.max_container_bytes:
            return fail(SignatureValidation.INVALID, f"Розмір контейнера має бути 1…{self.max_container_bytes} байт")
        try:
            p = parse_detached(container)
        except StructureError as exc:
            return fail(SignatureValidation.INVALID, str(exc))
        checks["structure"] = "ok"
        fmt = SignatureFormat.CADES_X_LONG if (p.has_timestamp and p.has_revocation_values) else SignatureFormat.CADES_BES
        try:
            rep = analyze_certificate(p.signer_cert_der)
            cert = x509.load_der_x509_certificate(p.signer_cert_der)
        except ValueError as exc:
            return fail(SignatureValidation.INVALID, f"Сертифікат підписувача не читається: {exc}")
        info = CertificateInfo(
            serial_hex=rep.serial_hex, issuer=rep.issuer, subject_cn=rep.subject_cn,
            valid_from=rep.valid_from, valid_to=rep.valid_to, is_qualified=rep.is_qualified,
            fingerprint_sha256=rep.fingerprint_sha256, tax_id=rep.tax_id)
        common: dict[str, object] = dict(signature_format=fmt, certificate=info, signed_at=p.signing_time,
                                         signed_digest_hex=p.message_digest.hex())

        if p.digest_algorithm in DSTU_DIGESTS or p.signature_algorithm in DSTU_SIGNATURES or rep.is_dstu:
            return await self._verify_dstu(container, p, rep, cert, common, checks, fail, expected_sha256,
                                           signer_user_id, not_before, now, content)

        if p.digest_algorithm != "sha256" or p.signature_algorithm not in _SIG_HASH:
            return fail(SignatureValidation.INVALID,
                        f"Непідтримуваний алгоритм {p.digest_algorithm}/{p.signature_algorithm}", **common)
        if not hmac.compare_digest(p.message_digest, bytes.fromhex(expected_sha256)):
            return fail(SignatureValidation.INVALID,
                        "Геш у підписі не збігається з гешем версії документа на погодженні", **common)
        checks["digest"] = "ok"
        if p.signing_cert_v2_hash is None:
            return fail(SignatureValidation.INVALID, "Немає атрибута signing-certificate-v2 (CAdES-BES)", **common)
        if p.signing_cert_v2_alg != "sha256" or not hmac.compare_digest(
                p.signing_cert_v2_hash, hashlib.sha256(p.signer_cert_der).digest()):
            return fail(SignatureValidation.INVALID, "signing-certificate-v2 не відповідає сертифікату", **common)
        if p.signing_time is None:
            return fail(SignatureValidation.INVALID, "Немає атрибута signing-time", **common)
        try:
            key = cert.public_key()
            h = _SIG_HASH[p.signature_algorithm]
            if isinstance(key, ec.EllipticCurvePublicKey) and p.signature_algorithm.endswith("ecdsa"):
                key.verify(p.signature, p.signed_attrs_der, ec.ECDSA(h))
            elif isinstance(key, rsa.RSAPublicKey) and p.signature_algorithm.endswith("rsa"):
                key.verify(p.signature, p.signed_attrs_der, padding.PKCS1v15(), h)
            else:
                return fail(SignatureValidation.INVALID, "Тип ключа не відповідає алгоритму підпису", **common)
        except (InvalidSignature, UnsupportedAlgorithm, ValueError):
            return fail(SignatureValidation.INVALID, "Підпис математично недійсний", **common)
        checks["signature"] = "ok"

        if self.internal is not None and self.internal.anchors.find_issuer(cert) is not None:
            return await self._finish_internal(cert, rep, p, common, checks, fail, signer_user_id, not_before, now)
        issuer = self.trust.find_issuer(cert)
        if issuer is None:
            return fail(SignatureValidation.INVALID, "Сертифікат видано недовіреним КНЕДП", **common)
        mock = is_mock_certificate(cert) or is_mock_certificate(issuer)
        if mock and not self.allow_mock:
            return fail(SignatureValidation.INVALID, "Сертифікат імітаційного КНЕДП заборонено в цьому середовищі",
                        **common)
        problem = self._qualified_problem(cert, rep)
        if problem:
            return fail(SignatureValidation.INVALID, problem, **common)
        checks["chain"] = "ok"
        time_fail = self._time_problem(rep, p.signing_time, not_before, now)
        if time_fail:
            return fail(*time_fail, **common)
        rev = await self.revocation.status(cert, issuer, now)
        checks["revocation"] = rev.value
        if rev is RevocationStatus.REVOKED:
            return fail(SignatureValidation.CERT_REVOKED, "Сертифікат відкликано або заблоковано", **common)
        if rev is not RevocationStatus.GOOD:
            return fail(SignatureValidation.INDETERMINATE,
                        "Не вдалося перевірити статус сертифіката (OCSP/CRL недоступні)", **common)
        owner = await self._owner_problem(rep, signer_user_id)
        if owner:
            return fail(SignatureValidation.INVALID, owner, **common)
        checks["signer_binding"] = "ok" if signer_user_id else "реєстрація: власника підтверджує виклик"
        return VerificationResult(status=SignatureValidation.VALID, reasons=(), checks=checks,
                                  level=CertLevel.MOCK if mock else CertLevel.QUALIFIED, **common)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ helpers

    def _qualified_problem(self, cert: x509.Certificate, rep: CertReport) -> str | None:
        if self.require_qualified and not rep.is_qualified:
            return "Сертифікат не кваліфікований (немає QcCompliance)"
        return _key_usage_problem(cert)

    def _time_problem(self, rep: CertReport, signing_time: datetime | None, not_before: datetime | None,
                      now: datetime) -> tuple[SignatureValidation, str] | None:
        if signing_time is None:
            return SignatureValidation.INVALID, "Немає часу підпису"
        st = signing_time if signing_time.tzinfo else signing_time.replace(tzinfo=timezone.utc)
        if not (rep.valid_from <= st <= rep.valid_to):
            return SignatureValidation.CERT_EXPIRED, "Підпис створено поза строком дії сертифіката"
        if not (rep.valid_from <= now <= rep.valid_to):
            return SignatureValidation.CERT_EXPIRED, "Строк дії сертифіката минув"
        if st > now + self.clock_skew:
            return SignatureValidation.INVALID, "Час підпису в майбутньому"
        if not_before is not None and st < not_before - self.clock_skew:
            return SignatureValidation.INVALID, "Підпис створено раніше, ніж документ надійшов учаснику на візування"
        return None

    async def _owner_problem(self, rep: CertReport, signer_user_id: uuid.UUID | None) -> str | None:
        if signer_user_id is None:
            return None
        if rep.tax_id is None:
            return "Сертифікат не містить РНОКПП підписувача"
        bound = await self.binding.matches(signer_user_id, rep.fingerprint_sha256, self.hasher(rep.tax_id))
        if bound is None:
            return "За учасником кроку не закріплено ключ КЕП"
        if not bound:
            return "Сертифікат належить іншій особі, ніж учасник кроку"
        return None

    async def _finish_internal(self, cert: x509.Certificate, rep: CertReport, p: ParsedSignature,
                               common: dict, checks: dict, fail: Fail, signer_user_id: uuid.UUID | None,
                               not_before: datetime | None, now: datetime) -> VerificationResult:
        assert self.internal is not None
        if not _has_policy(cert, INTERNAL_POLICY_OID):
            return fail(SignatureValidation.INVALID, "Сертифікат внутрішнього ЦСК без політики АІ-СЕД ПУЕТ", **common)
        ku = _key_usage_problem(cert)
        if ku:
            return fail(SignatureValidation.INVALID, ku, **common)
        checks["chain"] = "ok (внутрішній ЦСК АІ-СЕД ПУЕТ)"
        time_fail = self._time_problem(rep, p.signing_time, not_before, now)
        if time_fail:
            return fail(*time_fail, **common)
        if signer_user_id is not None and rep.sed_user_id != signer_user_id:
            return fail(SignatureValidation.INVALID, "Внутрішній ключ видано іншому користувачу СЕД", **common)
        rev = await self.internal.revocation.status_by_serial(rep.serial_hex, rep.sed_user_id)
        checks["revocation"] = rev.value
        if rev is RevocationStatus.REVOKED:
            return fail(SignatureValidation.CERT_REVOKED, "Внутрішній ключ відкликано", **common)
        if rev is not RevocationStatus.GOOD:
            return fail(SignatureValidation.INVALID, "Сертифікат не значиться в реєстрі виданих внутрішніх ключів",
                        **common)
        checks["signer_binding"] = ("ok (UID сертифіката = користувач СЕД)" if signer_user_id
                                    else "не перевірялось: підписувача не задано")
        return VerificationResult(status=SignatureValidation.VALID, reasons=(), checks=checks,
                                  level=CertLevel.INTERNAL, **common)

    async def _verify_dstu(self, container: bytes, p: ParsedSignature, rep: CertReport, cert: x509.Certificate,
                           common: dict, checks: dict, fail: Fail, expected_sha256: str,
                           signer_user_id: uuid.UUID | None, not_before: datetime | None, now: datetime,
                           content: ContentLoader | None) -> VerificationResult:
        if self.dstu is None or content is None:
            alg = DSTU_DIGESTS.get(p.digest_algorithm) or DSTU_SIGNATURES.get(p.signature_algorithm) or "ДСТУ 4145"
            return fail(SignatureValidation.INDETERMINATE,
                        f"Алгоритм {alg} потребує криптопровайдера КНЕДП (ІІТ / Дія.Підпис)", **common)
        data = await content()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            return fail(SignatureValidation.INDETERMINATE, "Вміст у сховищі не збігається з гешем версії", **common)
        try:
            ext = await self.dstu.verify_detached(container, data)
        except ExternalVerifierUnavailable:
            return fail(SignatureValidation.INDETERMINATE, "Сервіс перевірки ДСТУ-підписів недоступний", **common)
        if not ext.valid:
            return fail(SignatureValidation.INVALID, ext.reasons[0] if ext.reasons else "Підпис ДСТУ недійсний",
                        **common)
        if ext.signer_certificate_der and hashlib.sha256(ext.signer_certificate_der).hexdigest() != rep.fingerprint_sha256:
            return fail(SignatureValidation.INVALID, "Провайдер повернув інший сертифікат підписувача", **common)
        checks["digest"] = checks["signature"] = "ok (сертифікований провайдер)"
        if not ext.chain_valid or self.trust.find_issuer_structural(cert) is None:
            return fail(SignatureValidation.INVALID, "Сертифікат видано недовіреним КНЕДП", **common)
        problem = self._qualified_problem(cert, rep)
        if problem:
            return fail(SignatureValidation.INVALID, problem, **common)
        checks["chain"] = "ok"
        signing_time = ext.timestamp_time or ext.signing_time or p.signing_time
        time_fail = self._time_problem(rep, signing_time, not_before, now)
        if time_fail:
            return fail(*time_fail, **common)
        checks["revocation"] = ext.revocation.value
        if ext.revocation is RevocationStatus.REVOKED:
            return fail(SignatureValidation.CERT_REVOKED, "Сертифікат відкликано або заблоковано", **common)
        if ext.revocation is not RevocationStatus.GOOD:
            return fail(SignatureValidation.INDETERMINATE, "Не вдалося перевірити статус сертифіката", **common)
        owner = await self._owner_problem(rep, signer_user_id)
        if owner:
            return fail(SignatureValidation.INVALID, owner, **common)
        if ext.signature_format == SignatureFormat.CADES_X_LONG.value:
            common["signature_format"] = SignatureFormat.CADES_X_LONG
        common["signed_at"] = signing_time
        return VerificationResult(status=SignatureValidation.VALID, reasons=(), checks=checks,
                                  level=CertLevel.QUALIFIED, **common)  # type: ignore[arg-type]


def _key_usage_problem(cert: x509.Certificate) -> str | None:
    try:
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return "У сертифікаті немає keyUsage"
    if not (ku.digital_signature or ku.content_commitment):
        return "Ключ не призначений для електронного підпису"
    return None
