"""`python -m sed_routing.keytool` — робота з ключами на комп'ютері користувача (офлайн).

Особистий ключ і пароль нікуди не надсилаються. Команди:

  inspect  FILE            розпізнати файл-ключ (Key-6.dat, .jks ПриватБанку, .pfx/.p12, .pem)
                           або сертифікат; перевірити пароль і відповідність ключа сертифікату
  new-key  --key K --csr C згенерувати ключ ECDSA P-256 і CSR для внутрішнього ключа АІ-СЕД ПУЕТ
  sign     --key K FILE    підписати файл або геш (CAdES-BES detached) ключем .p12/.pem (ECDSA/RSA);
                           ключі ДСТУ 4145 підписуються лише віджетом ІІТ або Дія.Підпис
  verify   --sig S FILE    перевірити підпис офлайн (структура, геш, математика, ланцюжок, строк)
  ca-init  --out DIR       церемонія створення внутрішнього ЦСК (кореневий + видавальний)

Пароль: --password-env ЗМІННА, --password-file ФАЙЛ або запит у терміналі (не в аргументах).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from .kep.cades import build_detached_cades_bes
from .kep.certinfo import CertificateParseError, analyze_certificate, load_certificate_bytes
from .kep.internal_ca import bootstrap_ca
from .kep.keyfile import KeyFormat, detect_format, inspect_key_file
from .kep.models import PemTrustStore, RevocationStatus, TaxIdHasher, VerificationResult
from .kep.verifier import CadesVerifier, InternalTrust


def _password(args: argparse.Namespace, prompt: str = "Пароль до ключа: ", *, required: bool = True) -> str | None:
    if getattr(args, "password_env", None):
        v = os.environ.get(args.password_env)
        if v is None:
            sys.exit(f"Змінна {args.password_env} не задана")
        return v
    if getattr(args, "password_file", None):
        return Path(args.password_file).read_text().rstrip("\r\n")
    if not required and not sys.stdin.isatty():
        return None
    return getpass.getpass(prompt)


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------- inspect

def cmd_inspect(args: argparse.Namespace) -> int:
    data = Path(args.file).read_bytes()
    fmt = detect_format(data)
    if fmt is KeyFormat.CERTIFICATE or (fmt is KeyFormat.UNKNOWN and not args.ask_password):
        try:
            rep = analyze_certificate(load_certificate_bytes(data))
        except CertificateParseError as exc:
            _print({"format": fmt.value, "problems": [str(exc)]})
            return 1
        problem = rep.validity_problem()
        _print({"format": "certificate", "certificate": rep.public_dict(), "problems": [problem] if problem else []})
        return 1 if problem else 0
    pwd = _password(args, required=False) if fmt is not KeyFormat.UNKNOWN or args.ask_password else None
    rep = inspect_key_file(data, pwd)
    _print(rep.to_dict())
    return 0 if rep.ok else 1


# ---------------------------------------------------------------------- new-key

def cmd_new_key(args: argparse.Namespace) -> int:
    key_path, csr_path = Path(args.key), Path(args.csr)
    if key_path.exists() and not args.force:
        sys.exit(f"{key_path} уже існує (--force, щоб перезаписати)")
    pwd = _password(args, "Новий пароль до ключа: ")
    assert pwd is not None
    if len(pwd) < 12:
        sys.exit("Пароль має бути щонайменше 12 символів")
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.name or "AI-SED PUET user")]))
           .sign(key, hashes.SHA256()))
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(pwd.encode()))
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    csr_path.write_bytes(csr.public_bytes(serialization.Encoding.PEM))
    print(f"Ключ: {key_path} (зашифровано паролем, права 600)\nCSR: {csr_path} — надішліть у СЕД: "
          "«Мої ключі → Внутрішній ключ → За запитом CSR» (POST /api/v1/kep/internal/certificates/csr)")
    return 0


# ---------------------------------------------------------------------- sign

def _load_signing_key(path: Path, pwd: str, cert_path: str | None):  # noqa: ANN202
    data = path.read_bytes()
    fmt = detect_format(data)
    if fmt is KeyFormat.PKCS12:
        key, cert, _ = pkcs12.load_key_and_certificates(data, pwd.encode())
        if key is None or cert is None:
            sys.exit("У файлі PKCS#12 немає ключа або сертифіката")
        return key, cert
    if fmt in (KeyFormat.JKS, KeyFormat.KEY6_DAT):
        sys.exit("Ключ ДСТУ 4145 (Дія, ПриватБанк, Key-6.dat): підписуйте віджетом ІІТ або Дія.Підпис")
    key = serialization.load_pem_private_key(data, pwd.encode())
    if not cert_path:
        sys.exit("Для ключа .pem вкажіть сертифікат: --cert")
    return key, x509.load_der_x509_certificate(load_certificate_bytes(Path(cert_path).read_bytes()))


def _digest_arg(args: argparse.Namespace) -> str:
    if args.sha256:
        h = args.sha256.strip().lower()
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            sys.exit("--sha256 має бути 64 шістнадцяткові символи")
        return h
    if not args.file:
        sys.exit("Вкажіть файл або --sha256")
    return hashlib.sha256(Path(args.file).read_bytes()).hexdigest()


def cmd_sign(args: argparse.Namespace) -> int:
    pwd = _password(args)
    assert pwd is not None
    try:
        key, cert = _load_signing_key(Path(args.key), pwd, args.cert)
    except ValueError:
        sys.exit("Неправильний пароль або пошкоджений файл ключа")
    if cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != \
            key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
        sys.exit("Ключ не відповідає сертифікату")
    digest = _digest_arg(args)
    if isinstance(key, ec.EllipticCurvePrivateKey):
        alg, sign = "sha256_ecdsa", (lambda d: key.sign(d, ec.ECDSA(hashes.SHA256())))
    elif isinstance(key, rsa.RSAPrivateKey):
        alg, sign = "sha256_rsa", (lambda d: key.sign(d, padding.PKCS1v15(), hashes.SHA256()))
    else:
        sys.exit("Підтримуються ключі ECDSA та RSA")
    p7s = build_detached_cades_bes(cert_der=cert.public_bytes(serialization.Encoding.DER),
                                   content_sha256=bytes.fromhex(digest),
                                   signing_time=datetime.now(timezone.utc).replace(microsecond=0),
                                   sign=sign, signature_algorithm=alg)
    if args.out:
        Path(args.out).write_bytes(p7s)
        print(f"Підпис: {args.out} (SHA-256 вмісту {digest})")
    else:
        print(base64.b64encode(p7s).decode())
    return 0


# ---------------------------------------------------------------------- verify

class _Offline:
    """Офлайн: відкликання не перевіряється (це робить сервер СЕД)."""

    async def status(self, *_: object) -> RevocationStatus:
        return RevocationStatus.GOOD

    async def status_by_serial(self, *_: object) -> RevocationStatus:
        return RevocationStatus.GOOD

    async def matches(self, *_: object) -> bool | None:
        return None


def offline_verify(container: bytes, sha256_hex: str, anchors_pem: bytes | None) -> VerificationResult:
    trust = PemTrustStore.from_pem(anchors_pem) if anchors_pem else PemTrustStore([])
    off = _Offline()
    v = CadesVerifier(trust, off, off, TaxIdHasher(b"\0" * 32), internal=InternalTrust(trust, off),  # type: ignore[arg-type]
                      allow_mock=True, require_qualified=False)
    return asyncio.run(v.verify(container, expected_sha256=sha256_hex, signer_user_id=None, not_before=None))


def cmd_verify(args: argparse.Namespace) -> int:
    raw = Path(args.sig).read_bytes()
    try:
        container = raw if raw[:1] == b"\x30" else base64.b64decode(raw, validate=False)
    except ValueError:
        sys.exit("Підпис має бути DER (.p7s) або base64")
    r = offline_verify(container, _digest_arg(args), Path(args.ca).read_bytes() if args.ca else None)
    out = r.report()
    out["note"] = "Офлайн-перевірка: статус відкликання і закріплення ключа перевіряє сервер СЕД"
    _print(out)
    return 0 if r.ok else 1


# ---------------------------------------------------------------------- ca-init

def cmd_ca_init(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()) and not args.force:
        sys.exit(f"{out} не порожній")
    root_pwd = _password(args, "Пароль кореневого ключа (зберігається офлайн): ")
    issuing_pwd = os.environ.get(args.issuing_password_env or "", "") or getpass.getpass("Пароль видавального ключа: ")
    assert root_pwd is not None
    if min(len(root_pwd), len(issuing_pwd)) < 16:
        sys.exit("Паролі ключів ЦСК мають бути щонайменше 16 символів")
    b = bootstrap_ca()
    assert b.root_key is not None

    def write_key(name: str, key: ec.EllipticCurvePrivateKey, pwd: str) -> None:
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.BestAvailableEncryption(pwd.encode())))

    write_key("root.key.pem", b.root_key, root_pwd)
    write_key("issuing.key.pem", b.issuing_key, issuing_pwd)
    (out / "chain.pem").write_bytes(b.issuing_cert.public_bytes(serialization.Encoding.PEM)
                                    + b.root_cert.public_bytes(serialization.Encoding.PEM))
    print(f"Створено ЦСК у {out}: chain.pem, issuing.key.pem (на сервер, у Vault), "
          "root.key.pem (перенесіть на офлайн-носій і видаліть з цього комп'ютера)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="keytool", description="Ключі підпису АІ-СЕД ПУЕТ (офлайн)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def pw(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument("--password-env", help="Назва змінної середовища з паролем")
        g.add_argument("--password-file", help="Файл із паролем")

    p = sub.add_parser("inspect", help="Розпізнати й перевірити файл-ключ або сертифікат")
    p.add_argument("file")
    p.add_argument("--ask-password", action="store_true", help="Запитати пароль навіть для невідомого формату")
    pw(p)
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("new-key", help="Новий ключ і CSR для внутрішнього сертифіката")
    p.add_argument("--key", required=True)
    p.add_argument("--csr", required=True)
    p.add_argument("--name", help="Довільне ім'я в CSR (сервер підставить ПІБ з облікового запису)")
    p.add_argument("--force", action="store_true")
    pw(p)
    p.set_defaults(fn=cmd_new_key)

    p = sub.add_parser("sign", help="Підписати файл або геш (CAdES-BES detached)")
    p.add_argument("--key", required=True)
    p.add_argument("--cert", help="Сертифікат для ключа .pem")
    p.add_argument("file", nargs="?")
    p.add_argument("--sha256")
    p.add_argument("--out", help="Куди записати .p7s; без нього — base64 у stdout")
    pw(p)
    p.set_defaults(fn=cmd_sign)

    p = sub.add_parser("verify", help="Перевірити підпис офлайн")
    p.add_argument("--sig", required=True)
    p.add_argument("--ca", help="PEM з довіреними ЦСК (напр., /api/v1/kep/internal/ca.pem)")
    p.add_argument("file", nargs="?")
    p.add_argument("--sha256")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("ca-init", help="Створити внутрішній ЦСК")
    p.add_argument("--out", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--issuing-password-env")
    pw(p)
    p.set_defaults(fn=cmd_ca_init)

    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
