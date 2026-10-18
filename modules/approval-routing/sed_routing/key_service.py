"""Керування ключами підпису (use cases): КЕП з файлів-ключів КНЕДП і внутрішні ключі АІ-СЕД ПУЕТ.

* Кваліфікований ключ (Дія, ПриватБанк, інший КНЕДП) закріплюється за користувачем лише після
  доказу володіння: користувач підписує одноразовий виклик своїм ключем на своєму комп'ютері.
  Сам файл-ключ і пароль на сервер не передаються ніколи.
* Внутрішній ключ видає внутрішній ЦСК: за CSR (ключ генерується в користувача) або файлом
  PKCS#12, зашифрованим паролем користувача і відданим один раз.
Кожна дія пишеться в журнал аудиту (`sed_audit_event`).
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sed_db.models import OrgUnit, User

from . import views
from .domain.errors import AccessDenied, InvalidState, NotFound, RoutingError, SignatureRejected
from .kep.certinfo import LEVEL_LABELS_UK, CertificateParseError, CertLevel, analyze_certificate, load_certificate_bytes
from .kep.challenge import ChallengeError, ChallengeIssuer
from .kep.internal_ca import KEY_FILE_SUFFIX, POLICY_NOTICE, EnrollmentError, InternalCa, InternalSubject
from .kep.keyfile import looks_like_key_container
from .kep.registry import InternalCertRecord, InternalCertRegistry, KeyBinding, KeyRegistry
from .kep.verifier import CadesVerifier, is_mock_certificate
from .service import Caller, utcnow

REGISTRATION = "key-registration"
EXPIRY_WARNING = timedelta(days=30)


class PrivateKeyUploadRefused(RoutingError):
    http_status = 422
    code = "private_key_upload_refused"


class KeyRegistrationRejected(RoutingError):
    http_status = 422
    code = "key_registration_rejected"


class InternalCaDisabled(RoutingError):
    http_status = 404
    code = "internal_ca_disabled"


def _full_name(u: User) -> str:
    return " ".join(x for x in (u.first_name, u.patronymic, u.last_name.upper()) if x)


def _name_matches(cn: str, u: User) -> bool:
    """ПІБ у сертифікаті КНЕДП збігається з карткою користувача (прізвище та ім'я, без регістру)."""
    words = {w.strip(".,").casefold().replace("’", "'") for w in cn.split()}
    return all(x.casefold().replace("’", "'") in words for x in (u.last_name, u.first_name))


class KeyManagementService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        verifier: CadesVerifier,
        keys: KeyRegistry,
        internal_certs: InternalCertRegistry,
        challenges: ChallengeIssuer,
        ca: InternalCa | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        admin_role: str = "sed-kep-admin",
        require_name_match: bool = True,
    ) -> None:
        self.sessionmaker, self.verifier, self.keys = sessionmaker, verifier, keys
        self.internal_certs, self.challenges, self.ca = internal_certs, challenges, ca
        self.clock, self.admin_role, self.require_name_match = clock, admin_role, require_name_match

    # ------------------------------------------------------------------ helpers

    async def _user(self, caller: Caller) -> User:
        async with self.sessionmaker() as s:
            u = await s.scalar(select(User).where(User.keycloak_sub == caller.keycloak_sub))
        if u is None or not u.is_active:
            raise AccessDenied("Користувача не знайдено або його вимкнено")
        return u

    async def _audit(self, actor: uuid.UUID, action: str, target_id: uuid.UUID | None, details: dict) -> None:
        async with self.sessionmaker() as s, s.begin():
            await s.execute(text("SELECT set_config('sed.actor_user_id', :u, true), "
                                 "set_config('sed.actor_type', 'user', true)"), {"u": str(actor)})
            await s.execute(text("SELECT sed_audit_event(:a, 'user_kep_identities', :t, NULL, CAST(:d AS jsonb))"),
                            {"a": action, "t": target_id, "d": json.dumps(details, ensure_ascii=False, default=str)})

    def _is_admin(self, caller: Caller) -> bool:
        return self.admin_role in caller.roles

    def _view(self, b: KeyBinding) -> views.KeyBindingView:
        return views.KeyBindingView(
            id=b.id, level=b.level.value, level_uk=LEVEL_LABELS_UK[b.level], subject_cn=b.cert_subject_cn,  # type: ignore[arg-type]
            issuer=b.cert_issuer, serial=b.cert_serial, fingerprint_sha256=b.fingerprint_sha256,
            valid_from=b.cert_valid_from, valid_to=b.cert_valid_to, key_format=b.key_format,
            bound_via=b.bound_via, bound_at=b.bound_at, revoked_at=b.revoked_at, revoke_reason=b.revoke_reason,
            expires_soon=b.active and b.cert_valid_to - self.clock() < EXPIRY_WARNING)

    def _require_ca(self) -> InternalCa:
        if self.ca is None:
            raise InternalCaDisabled("Внутрішній ЦСК АІ-СЕД ПУЕТ не налаштовано")
        return self.ca

    # ------------------------------------------------------------------ КЕП КНЕДП

    async def challenge(self, caller: Caller) -> views.ChallengeView:
        user = await self._user(caller)
        c = self.challenges.issue(user.id, REGISTRATION, self.clock())
        return views.ChallengeView(
            challenge_token=c.token, data_base64=base64.b64encode(c.data).decode(), data_text=c.data.decode(),
            content_sha256=c.sha256, expires_at=c.expires_at,
            instructions_uk="Підпишіть data_base64 як файл своїм ключем (віджет ІІТ «Підписати файл», Дія.Підпис "
                            "або `keytool sign`) у форматі CAdES detached і надішліть підпис разом із challenge_token. "
                            "Файл-ключ і пароль залишаються на вашому комп'ютері.")

    async def register(self, caller: Caller, req: views.KeyRegisterRequest) -> views.KeyBindingView:
        user = await self._user(caller)
        now = self.clock()
        try:
            ch = self.challenges.open(req.challenge_token, user.id, REGISTRATION, now)
            container = base64.b64decode(req.signature_base64, validate=True)
        except ChallengeError as exc:
            raise KeyRegistrationRejected(str(exc)) from None
        except (binascii.Error, ValueError):
            raise KeyRegistrationRejected("Підпис має бути в base64 (DER-контейнер CAdES)") from None

        async def content() -> bytes:
            return ch.data

        result = await self.verifier.verify(container, expected_sha256=ch.sha256, signer_user_id=None,
                                            not_before=None, now=now, content=content)
        if not result.ok:
            await self._audit(user.id, "kep.key_registration_rejected", None, result.report())
            raise SignatureRejected(result.reasons[0], validation_status=result.status.value,
                                    reasons=list(result.reasons))
        cert = result.certificate
        assert cert is not None
        if result.level is CertLevel.INTERNAL:
            raise KeyRegistrationRejected("Внутрішній ключ закріплюється автоматично під час видачі")
        if result.level not in (CertLevel.QUALIFIED, CertLevel.MOCK):
            raise KeyRegistrationRejected("Закріпити можна лише кваліфікований сертифікат КНЕДП")
        if not cert.tax_id:
            raise KeyRegistrationRejected("Сертифікат не містить РНОКПП: такий ключ не можна закріпити")
        if self.require_name_match and not _name_matches(cert.subject_cn, user):
            raise KeyRegistrationRejected("ПІБ у сертифікаті не збігається з ПІБ у вашій картці користувача")
        tax_hmac = self.verifier.hasher(cert.tax_id)

        existing = await self.keys.by_fingerprint(cert.fingerprint_sha256)
        if existing is not None:
            if existing.user_id != user.id:
                await self._audit(user.id, "kep.key_registration_rejected", existing.id,
                                  {"reason": "certificate bound to another user", "sha256": cert.fingerprint_sha256})
                raise KeyRegistrationRejected("Цей сертифікат уже закріплено за іншим користувачем")
            return self._view(existing)  # повторна реєстрація того самого ключа — без змін
        mine = [b for b in await self.keys.for_user(user.id) if b.level is CertLevel.QUALIFIED]
        if any(b.tax_id_hmac and b.tax_id_hmac != tax_hmac for b in mine):
            raise KeyRegistrationRejected("РНОКПП у сертифікаті відрізняється від уже закріпленого ключа")
        binding = await self.keys.add(KeyBinding(
            user_id=user.id, level=CertLevel.QUALIFIED, fingerprint_sha256=cert.fingerprint_sha256,
            cert_serial=cert.serial_hex, cert_issuer=cert.issuer, cert_subject_cn=cert.subject_cn,
            cert_valid_from=cert.valid_from, cert_valid_to=cert.valid_to, bound_via="proof_of_possession",
            tax_id_hmac=tax_hmac, key_format=req.key_format, bound_at=now))
        await self._audit(user.id, "kep.key_registered", binding.id, {
            "level": result.level.value if result.level else None, "serial": cert.serial_hex,
            "issuer": cert.issuer, "sha256": cert.fingerprint_sha256, "key_format": req.key_format})
        return self._view(binding)

    async def my_keys(self, caller: Caller, *, include_revoked: bool = False) -> list[views.KeyBindingView]:
        user = await self._user(caller)
        return [self._view(b) for b in await self.keys.for_user(user.id, active_only=not include_revoked)]

    async def revoke_key(self, caller: Caller, binding_id: uuid.UUID, req: views.KeyRevokeRequest) -> views.KeyBindingView:
        """Відкріпити ключ (втрата, звільнення, компрометація). Для внутрішнього — ще й відкликати сертифікат."""
        user = await self._user(caller)
        b = await self.keys.get(binding_id)
        if b is None:
            raise NotFound("Ключ не знайдено")
        if b.user_id != user.id and not self._is_admin(caller):
            raise AccessDenied("Відкріпити ключ може лише його власник або адміністратор ключів")
        now = self.clock()
        out = await self.keys.revoke(binding_id, now, req.reason)
        if out is None:
            raise InvalidState("Ключ уже відкріплено")
        if b.level is CertLevel.INTERNAL:
            await self.internal_certs.revoke(b.cert_serial, now, "key_compromise" if "компром" in req.reason.casefold()
                                             else "cessation_of_operation", user.id)
        await self._audit(user.id, "kep.key_revoked", binding_id, {"reason": req.reason, "serial": b.cert_serial,
                                                                   "level": b.level.value})
        return self._view(out)

    async def inspect_certificate(self, caller: Caller, data: bytes) -> views.CertificateCheckView:
        """Перевірка сертифіката (.cer/.crt/.pem/base64) — без особистого ключа."""
        user = await self._user(caller)
        if looks_like_key_container(data):
            raise PrivateKeyUploadRefused(
                "Це файл з особистим ключем. Не надсилайте його на сервер: перевірте ключ локально "
                "(`keytool inspect`) або завантажте лише сертифікат (.cer)")
        try:
            der = load_certificate_bytes(data)
            rep = analyze_certificate(der)
        except CertificateParseError as exc:
            raise RoutingError(str(exc)) from None
        now = self.clock()
        problems = [p for p in [rep.validity_problem(now)] if p]
        trusted: bool | None
        if rep.is_dstu:
            cert = x509.load_der_x509_certificate(der)
            trusted = None if self.verifier.trust.find_issuer_structural(cert) else False
        else:
            cert = x509.load_der_x509_certificate(der)
            internal = self.verifier.internal.anchors if self.verifier.internal else None
            trusted = bool(self.verifier.trust.find_issuer(cert) or (internal and internal.find_issuer(cert)))
            if is_mock_certificate(cert) and not self.verifier.allow_mock:
                problems.append("Сертифікат імітаційного КНЕДП")
        if trusted is False:
            problems.append("Видавець не входить до довірених КНЕДП / внутрішнього ЦСК")
        if rep.is_ca:
            problems.append("Це сертифікат центру сертифікації, а не користувача")
        if rep.level is CertLevel.QUALIFIED and not rep.tax_id:
            problems.append("Немає РНОКПП: ключ не можна закріпити")
        if not {"digital_signature", "non_repudiation", "content_commitment"} & set(rep.key_usage):
            problems.append("Ключ не призначений для електронного підпису (keyUsage)")
        b = await self.keys.by_fingerprint(rep.fingerprint_sha256)
        return views.CertificateCheckView(
            level=rep.level.value, level_uk=LEVEL_LABELS_UK[rep.level], certificate=rep.public_dict(),  # type: ignore[arg-type]
            usable_for_signing=not problems, problems=problems, trusted_issuer=trusted,
            registered_to_me=b is not None and b.user_id == user.id)

    async def verify_signature(self, caller: Caller, req: views.SignatureVerifyRequest) -> views.SignatureVerifyView:
        """Перевірити довільний підпис над гешем (без прив'язки до кроку). ДСТУ-підпис потребує вмісту,
        тож для нього результат «невизначений» — перевіряйте через крок або через віджет ІІТ."""
        await self._user(caller)
        try:
            container = base64.b64decode(req.signature_base64, validate=True)
        except (binascii.Error, ValueError):
            raise RoutingError("Підпис має бути в base64") from None
        r = await self.verifier.verify(container, expected_sha256=req.content_sha256, signer_user_id=None,
                                       not_before=None, now=self.clock())
        return views.SignatureVerifyView(status=r.status, level=r.level.value if r.level else None,  # type: ignore[arg-type]
                                         reasons=list(r.reasons), report=r.report())

    # ------------------------------------------------------------------ внутрішні ключі

    async def _subject(self, user: User) -> InternalSubject:
        unit = None
        if user.primary_org_unit_id:
            async with self.sessionmaker() as s:
                unit = await s.scalar(select(OrgUnit.name_uk).where(OrgUnit.id == user.primary_org_unit_id))
        return InternalSubject(user.id, _full_name(user), user.email, (unit or "")[:64] or None)

    async def _register_internal(self, user: User, cert: x509.Certificate, delivery: str) -> views.InternalCertView:
        ca = self._require_ca()
        now = self.clock()
        der = cert.public_bytes(serialization.Encoding.DER)
        rep = analyze_certificate(der)
        for old in await self.internal_certs.active_for_user(user.id):  # один чинний внутрішній ключ
            await self.internal_certs.revoke(old.serial_hex, now, "superseded", user.id)
        for b in await self.keys.for_user(user.id):
            if b.level is CertLevel.INTERNAL:
                await self.keys.revoke(b.id, now, "Замінено новим внутрішнім ключем")
        await self.internal_certs.add(InternalCertRecord(
            serial_hex=rep.serial_hex, user_id=user.id, fingerprint_sha256=rep.fingerprint_sha256, cert_der=der,
            not_before=rep.valid_from, not_after=rep.valid_to, delivery=delivery, issued_at=now))
        binding = await self.keys.add(KeyBinding(
            user_id=user.id, level=CertLevel.INTERNAL, fingerprint_sha256=rep.fingerprint_sha256,
            cert_serial=rep.serial_hex, cert_issuer=rep.issuer, cert_subject_cn=rep.subject_cn,
            cert_valid_from=rep.valid_from, cert_valid_to=rep.valid_to, bound_via="internal_issuance",
            key_format="pkcs12" if delivery == "server_p12" else "csr", bound_at=now))
        await self._audit(user.id, "kep.internal_key_issued", binding.id,
                          {"serial": rep.serial_hex, "delivery": delivery, "sha256": rep.fingerprint_sha256})
        return views.InternalCertView(
            serial=rep.serial_hex, subject_cn=rep.subject_cn, fingerprint_sha256=rep.fingerprint_sha256,
            valid_from=rep.valid_from, valid_to=rep.valid_to,
            certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
            chain_pem=ca.chain_pem.decode(), notice_uk=POLICY_NOTICE)

    async def enroll_csr(self, caller: Caller, req: views.CsrEnrollRequest) -> views.InternalCertView:
        user = await self._user(caller)
        try:
            cert = self._require_ca().issue_from_csr(req.csr_pem.encode(), await self._subject(user), self.clock())
        except EnrollmentError as exc:
            raise KeyRegistrationRejected(str(exc)) from None
        return await self._register_internal(user, cert, "csr")

    async def issue_key_file(self, caller: Caller, req: views.KeyFileRequest) -> tuple[bytes, str]:
        """Файл-ключ PKCS#12 (віддається один раз; копії на сервері немає)."""
        user = await self._user(caller)
        try:
            blob, cert = self._require_ca().generate_key_file(await self._subject(user), req.password, self.clock())
        except EnrollmentError as exc:
            raise KeyRegistrationRejected(str(exc)) from None
        await self._register_internal(user, cert, "server_p12")
        return blob, f"{user.last_name}-{format(cert.serial_number, 'X')[:8]}{KEY_FILE_SUFFIX}"

    async def revoke_internal(self, caller: Caller, serial_hex: str, req: views.KeyRevokeRequest) -> None:
        user = await self._user(caller)
        rec = await self.internal_certs.get(serial_hex.upper())
        if rec is None:
            raise NotFound("Сертифікат не знайдено")
        if rec.user_id != user.id and not self._is_admin(caller):
            raise AccessDenied("Відкликати ключ може лише його власник або адміністратор ключів")
        now = self.clock()
        reason = "key_compromise" if "компром" in req.reason.casefold() else "cessation_of_operation"
        if await self.internal_certs.revoke(rec.serial_hex, now, reason, user.id) is None:
            raise InvalidState("Сертифікат уже відкликано")
        b = await self.keys.by_fingerprint(rec.fingerprint_sha256)
        if b is not None:
            await self.keys.revoke(b.id, now, req.reason)
        await self._audit(user.id, "kep.internal_key_revoked", b.id if b else None,
                          {"serial": rec.serial_hex, "reason": req.reason})

    def ca_chain_pem(self) -> bytes:
        return self._require_ca().chain_pem

    async def crl(self) -> bytes:
        ca = self._require_ca()
        reasons = {"key_compromise": x509.ReasonFlags.key_compromise, "superseded": x509.ReasonFlags.superseded,
                   "cessation_of_operation": x509.ReasonFlags.cessation_of_operation}
        revoked = [(int(r.serial_hex, 16), r.revoked_at, reasons.get(r.revocation_reason or ""))
                   for r in await self.internal_certs.revoked() if r.revoked_at]
        return ca.crl(revoked, self.clock())
