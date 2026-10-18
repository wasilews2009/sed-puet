"""Файлові ключі: розпізнавання формату, перевірка пароля і сертифікатів — ЛОКАЛЬНО.

Підтримувані контейнери (формат визначається за вмістом, не за розширенням):

| Формат | Хто видає | Що вміємо без бібліотеки ІІТ |
|---|---|---|
| JKS (`*.jks`) | АЦСК ПриватБанк, частина КНЕДП | сертифікати, перевірка пароля (цілісність сховища й ключа), алгоритм ключа |
| PKCS#12 (`*.pfx`, `*.p12`) | Дія, ДПС, інші КНЕДП; внутрішній ключ СЕД | перевірка пароля (MAC), сертифікати з відкритих контейнерів; ECDSA/RSA — повне відкриття |
| Key-6.dat | ІІТ-сумісні КНЕДП (зокрема Дія) | розпізнавання; пароль і ключ перевіряє лише бібліотека ІІТ / віджет |

Особистий ключ КЕП має залишатися під одноосібним контролем підписувача, тому цей модуль
призначено для утиліти `keytool` на комп'ютері користувача та для віджета підпису в браузері.
API сервера файлів ключів не приймає (див. `looks_like_key_container`).
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass, field
from enum import StrEnum

from asn1crypto import cms, core, keys, pkcs12
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12 as crypto_pkcs12

from .certinfo import DSTU4145_KEY_OIDS, CertificateParseError, CertReport, analyze_certificate

IIT_ARC = "1.3.6.1.4.1.19398"
JKS_MAGIC = 0xFEEDFEED
JKS_KEY_PROTECTOR_OID = "1.3.6.1.4.1.42.2.17.1.1"
GOST_OIDS_PREFIX = "1.2.804.2.1.1.1"


class KeyFormat(StrEnum):
    JKS = "jks"
    PKCS12 = "pkcs12"
    KEY6_DAT = "key6_dat"
    PKCS8 = "pkcs8"
    CERTIFICATE = "certificate"
    UNKNOWN = "unknown"


FORMAT_LABELS_UK = {
    KeyFormat.JKS: "Java KeyStore (JKS) — формат ключів ПриватБанку",
    KeyFormat.PKCS12: "PKCS#12 (PFX/P12)",
    KeyFormat.KEY6_DAT: "Key-6.dat (формат ІІТ)",
    KeyFormat.PKCS8: "PKCS#8 (зашифрований особистий ключ)",
    KeyFormat.CERTIFICATE: "Сертифікат (без особистого ключа)",
    KeyFormat.UNKNOWN: "Невідомий формат",
}


@dataclass
class KeyFileReport:
    format: KeyFormat
    password_ok: bool | None = None          # None — перевірити неможливо без бібліотеки ІІТ
    key_algorithm: str | None = None
    key_matches_certificate: bool | None = None
    certificates: list[CertReport] = field(default_factory=list)
    requires_certified_library: bool = False  # ДСТУ: підписувати лише віджетом/бібліотекою ІІТ
    can_sign_locally: bool = False            # ECDSA/RSA: keytool може підписати сам
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def signer_certificate(self) -> CertReport | None:
        leaf = [c for c in self.certificates if not c.is_ca]
        sig = [c for c in leaf if "digital_signature" in c.key_usage or "non_repudiation" in c.key_usage]
        return (sig or leaf or [None])[0]

    @property
    def ok(self) -> bool:
        return not self.problems and self.password_ok is not False

    def to_dict(self) -> dict[str, object]:
        return {
            "format": self.format.value, "format_uk": FORMAT_LABELS_UK[self.format],
            "password_ok": self.password_ok, "key_algorithm": self.key_algorithm,
            "key_matches_certificate": self.key_matches_certificate,
            "requires_certified_library": self.requires_certified_library,
            "can_sign_locally": self.can_sign_locally, "problems": self.problems, "notes": self.notes,
            "certificates": [c.public_dict() for c in self.certificates],
        }


# ---------------------------------------------------------------------- detection

def _der_seq(data: bytes) -> core.Sequence | None:
    try:
        return core.Sequence.load(data, strict=True) if data[:1] == b"\x30" else None
    except ValueError:
        return None


def detect_format(data: bytes) -> KeyFormat:
    if len(data) >= 4 and struct.unpack(">I", data[:4])[0] == JKS_MAGIC:
        return KeyFormat.JKS
    if data.lstrip().startswith(b"-----BEGIN CERTIFICATE"):
        return KeyFormat.CERTIFICATE
    if b"PRIVATE KEY-----" in data[:200]:
        return KeyFormat.PKCS8
    seq = _der_seq(data)
    if seq is None:
        return KeyFormat.UNKNOWN
    try:
        pkcs12.Pfx.load(data, strict=True)["auth_safe"]["content_type"].native  # noqa: B018
        if core.Integer.load(seq[0].dump()).native == 3:
            return KeyFormat.PKCS12
    except (ValueError, TypeError, KeyError):
        pass
    try:
        epki = keys.EncryptedPrivateKeyInfo.load(data, strict=True)
        oid = epki["encryption_algorithm"]["algorithm"].dotted
        return KeyFormat.KEY6_DAT if oid.startswith((IIT_ARC, GOST_OIDS_PREFIX)) else KeyFormat.PKCS8
    except (ValueError, TypeError, KeyError):
        pass
    if IIT_ARC.encode() in data[:64] or _contains_oid(data[:96], IIT_ARC):
        return KeyFormat.KEY6_DAT
    try:
        analyze_certificate(data)
        return KeyFormat.CERTIFICATE
    except CertificateParseError:
        return KeyFormat.UNKNOWN


def _contains_oid(blob: bytes, dotted_prefix: str) -> bool:
    enc = core.ObjectIdentifier(dotted_prefix).dump()[2:]  # тіло OID без тегу й довжини
    return enc in blob


def looks_like_key_container(data: bytes) -> bool:
    return detect_format(data) in {KeyFormat.JKS, KeyFormat.PKCS12, KeyFormat.KEY6_DAT, KeyFormat.PKCS8}


# ---------------------------------------------------------------------- JKS

def _jks_utf(buf: bytes, pos: int) -> tuple[str, int]:
    (n,) = struct.unpack(">H", buf[pos:pos + 2])
    return buf[pos + 2:pos + 2 + n].decode("utf-8", errors="replace"), pos + 2 + n


def _jks_password(password: str) -> bytes:
    return password.encode("utf-16-be")


def _jks_unprotect(protected: bytes, password: str) -> bytes | None:
    """Sun KeyProtector: SHA-1-гамування; повертає PKCS#8 або None, якщо пароль хибний."""
    try:
        info = keys.EncryptedPrivateKeyInfo.load(protected)
        if info["encryption_algorithm"]["algorithm"].dotted != JKS_KEY_PROTECTOR_OID:
            return None
        blob = info["encrypted_data"].native
    except ValueError:
        return None
    salt, enc, check = blob[:20], blob[20:-20], blob[-20:]
    pw = _jks_password(password)
    stream, digest = bytearray(), salt
    while len(stream) < len(enc):
        digest = hashlib.sha1(pw + digest).digest()
        stream.extend(digest)
    plain = bytes(a ^ b for a, b in zip(enc, stream, strict=False))
    return plain if hmac.compare_digest(hashlib.sha1(pw + plain).digest(), check) else None


def _pkcs8_algorithm(pkcs8: bytes) -> tuple[str, bool]:
    try:
        pki = keys.PrivateKeyInfo.load(pkcs8)
        oid = pki["private_key_algorithm"]["algorithm"].dotted
    except ValueError:
        return "невідомий", False
    if oid in DSTU4145_KEY_OIDS:
        return "ДСТУ 4145-2002", True
    return {"1.2.840.10045.2.1": "ECDSA", "1.2.840.113549.1.1.1": "RSA"}.get(oid, oid), False


def inspect_jks(data: bytes, password: str | None) -> KeyFileReport:
    r = KeyFileReport(KeyFormat.JKS)
    try:
        _, version, count = struct.unpack(">III", data[:12])
        pos, protected_keys = 12, []
        for _ in range(count):
            (tag,) = struct.unpack(">I", data[pos:pos + 4])
            alias, pos = _jks_utf(data, pos + 4)
            pos += 8  # timestamp
            if tag == 1:
                (n,) = struct.unpack(">I", data[pos:pos + 4])
                protected_keys.append((alias, data[pos + 4:pos + 4 + n]))
                pos += 4 + n
                (chain,) = struct.unpack(">I", data[pos:pos + 4])
                pos += 4
                for _ in range(chain):
                    if version == 2:
                        _, pos = _jks_utf(data, pos)
                    (n,) = struct.unpack(">I", data[pos:pos + 4])
                    r.certificates.append(analyze_certificate(data[pos + 4:pos + 4 + n]))
                    pos += 4 + n
            elif tag == 2:
                if version == 2:
                    _, pos = _jks_utf(data, pos)
                (n,) = struct.unpack(">I", data[pos:pos + 4])
                r.certificates.append(analyze_certificate(data[pos + 4:pos + 4 + n]))
                pos += 4 + n
            else:
                raise ValueError(f"невідомий тип запису {tag}")
        body, mac = data[:pos], data[pos:pos + 20]
    except (struct.error, ValueError, CertificateParseError) as exc:
        r.problems.append(f"Пошкоджений JKS: {exc}")
        return r
    if not protected_keys:
        r.problems.append("У сховищі немає особистого ключа")
    if password is None:
        r.notes.append("Пароль не вказано: показано лише сертифікати")
        return r
    pw = _jks_password(password)
    store_ok = hmac.compare_digest(hashlib.sha1(pw + b"Mighty Aphrodite" + body).digest(), mac)
    plain = next((p for _, k in protected_keys if (p := _jks_unprotect(k, password)) is not None), None)
    r.password_ok = plain is not None
    if plain is None:
        r.problems.append("Неправильний пароль до ключа")
    elif not store_ok:
        r.notes.append("Пароль ключа правильний, але цілісність сховища захищена іншим паролем")
    if plain is not None:
        r.key_algorithm, r.requires_certified_library = _pkcs8_algorithm(plain)
        if not r.requires_certified_library:
            r.key_matches_certificate = _match_pkcs8(plain, r.certificates)
            r.can_sign_locally = bool(r.key_matches_certificate)
    if r.requires_certified_library:
        r.notes.append("Ключ ДСТУ 4145: підписувати віджетом/бібліотекою ІІТ (сертифікований засіб КЕП)")
    return r


def _match_pkcs8(pkcs8: bytes, certs: list[CertReport]) -> bool | None:
    try:
        key = serialization.load_der_private_key(pkcs8, None)
    except (ValueError, TypeError):
        return None
    return _key_matches(key, certs)


def _key_matches(key, certs: list[CertReport]) -> bool | None:  # noqa: ANN001
    from cryptography import x509

    if not isinstance(key, ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey):
        return None
    pub = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    for c in certs:
        if not c.der or c.is_dstu:
            continue
        cert_pub = x509.load_der_x509_certificate(c.der).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if cert_pub == pub:
            return True
    return False


# ---------------------------------------------------------------------- PKCS#12

_P12_HASH = {"sha1": (hashlib.sha1, 20, 64), "sha256": (hashlib.sha256, 32, 64), "sha512": (hashlib.sha512, 64, 128)}


def _p12_kdf(password: str, salt: bytes, iterations: int, id_byte: int, size: int, algo: str) -> bytes:
    """RFC 7292, додаток B.2 (ключ для MAC: ID = 3)."""
    hfn, u, v = _P12_HASH[algo]
    pw = (password.encode("utf-16-be") + b"\x00\x00") if password is not None else b""
    d = bytes([id_byte]) * v

    def fill(b: bytes) -> bytes:
        if not b:
            return b""
        n = v * ((len(b) + v - 1) // v)
        return (b * (n // len(b) + 1))[:n]

    i_blk = fill(salt) + fill(pw)
    out = b""
    while len(out) < size:
        a = d + i_blk
        a = hfn(a).digest()
        for _ in range(iterations - 1):
            a = hfn(a).digest()
        out += a
        b = (a * (v // u + 1))[:v]
        b_int = int.from_bytes(b, "big") + 1
        new = bytearray()
        for j in range(0, len(i_blk), v):
            chunk = (int.from_bytes(i_blk[j:j + v], "big") + b_int) % (1 << (8 * v))
            new.extend(chunk.to_bytes(v, "big"))
        i_blk = bytes(new)
    return out[:size]


def p12_password_ok(data: bytes, password: str) -> bool | None:
    """Перевірка MAC контейнера. None — MAC відсутній або алгоритм ГОСТ (лише ІІТ)."""
    try:
        pfx = pkcs12.Pfx.load(data)
        mac = pfx["mac_data"]
        if mac.native is None:
            return None
        algo = mac["mac"]["digest_algorithm"]["algorithm"].native
        if algo not in _P12_HASH:
            return None
        content = pfx["auth_safe"]["content"].native
        hfn, u, _ = _P12_HASH[algo]
        key = _p12_kdf(password, mac["mac_salt"].native, mac["iterations"].native, 3, u, algo)
        calc = hmac.new(key, content, hfn).digest()
        return hmac.compare_digest(calc, mac["mac"]["digest"].native)
    except (ValueError, TypeError, KeyError):
        return None


def _p12_open_certs(data: bytes) -> list[bytes]:
    """Сертифікати з незашифрованих SafeContents (у ключах ДСТУ вони часто відкриті)."""
    out = []
    try:
        pfx = pkcs12.Pfx.load(data)
        for ci in pfx.authenticated_safe:
            if ci["content_type"].native != "data":
                continue
            for bag in pkcs12.SafeContents.load(ci["content"].native):
                if bag["bag_id"].native == "cert_bag" and bag["bag_value"]["cert_id"].native == "x509":
                    out.append(bag["bag_value"]["cert_value"].parsed.dump())
    except (ValueError, TypeError, KeyError):
        pass
    return out


def inspect_pkcs12(data: bytes, password: str | None) -> KeyFileReport:
    r = KeyFileReport(KeyFormat.PKCS12)
    if password is not None:
        try:
            key, cert, extra = crypto_pkcs12.load_key_and_certificates(data, password.encode())
            r.password_ok = True
            ders = [c.public_bytes(serialization.Encoding.DER) for c in ([cert] if cert else []) + list(extra or [])]
            r.certificates = [analyze_certificate(d) for d in ders]
            if key is not None:
                r.key_algorithm = "ECDSA" if isinstance(key, ec.EllipticCurvePrivateKey) else (
                    "RSA" if isinstance(key, rsa.RSAPrivateKey) else type(key).__name__)
                r.key_matches_certificate = _key_matches(key, r.certificates)
                r.can_sign_locally = bool(r.key_matches_certificate)
            else:
                r.problems.append("У контейнері немає особистого ключа")
            return r
        except ValueError:
            r.password_ok = p12_password_ok(data, password)
        except Exception as exc:  # noqa: BLE001 — UnsupportedAlgorithm та ін. для ДСТУ
            r.notes.append(f"Контейнер не відкривається стандартною криптографією: {type(exc).__name__}")
            r.password_ok = p12_password_ok(data, password)
    r.certificates = [analyze_certificate(d) for d in _p12_open_certs(data)]
    if r.password_ok is not False:
        r.requires_certified_library = True
        r.key_algorithm = r.key_algorithm or ("ДСТУ 4145-2002" if any(c.is_dstu for c in r.certificates) else None)
        r.notes.append("Ключ відкривається лише бібліотекою ІІТ / віджетом підпису (алгоритми ДСТУ)")
    if r.password_ok is False:
        r.problems.append("Неправильний пароль до ключа")
    return r


def inspect_key_file(data: bytes, password: str | None = None) -> KeyFileReport:
    fmt = detect_format(data)
    if fmt is KeyFormat.JKS:
        return inspect_jks(data, password)
    if fmt is KeyFormat.PKCS12:
        return inspect_pkcs12(data, password)
    if fmt is KeyFormat.KEY6_DAT:
        return KeyFileReport(
            KeyFormat.KEY6_DAT, requires_certified_library=True, key_algorithm="ДСТУ 4145-2002",
            notes=["Key-6.dat шифрується ГОСТ 28147-89: пароль і ключ перевіряє бібліотека ІІТ / віджет підпису",
                   "Сертифікати до Key-6.dat зазвичай видаються окремими файлами (*.cer) — перевірте їх окремо"])
    if fmt is KeyFormat.CERTIFICATE:
        from .certinfo import load_certificate_bytes

        der = load_certificate_bytes(data)
        return KeyFileReport(KeyFormat.CERTIFICATE, certificates=[analyze_certificate(der)],
                             notes=["Це сертифікат: особистого ключа у файлі немає"])
    if fmt is KeyFormat.PKCS8:
        return KeyFileReport(KeyFormat.PKCS8, notes=["Окремий особистий ключ без сертифіката"])
    return KeyFileReport(KeyFormat.UNKNOWN, problems=["Формат файлу не розпізнано"])


def certificates_from_cms(container: bytes) -> list[bytes]:
    try:
        sd = cms.ContentInfo.load(container)["content"]
        return [c.chosen.dump() for c in sd["certificates"] or [] if c.name == "certificate"]
    except (ValueError, TypeError, KeyError):
        return []
