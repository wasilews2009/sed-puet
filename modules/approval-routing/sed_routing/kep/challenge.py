"""Безстанові виклики для доказу володіння ключем (proof of possession).

Сервер видає підписаний HMAC токен із випадковим nonce і строком дії. Користувач підписує
байти виклику своїм ключем (у віджеті ІІТ або `keytool`), сервер перевіряє підпис і токен.
Особистий ключ при цьому не залишає пристрій користувача.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class ChallengeError(ValueError):
    pass


@dataclass(frozen=True)
class Challenge:
    token: str
    data: bytes          # що саме підписати
    expires_at: datetime

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class ChallengeIssuer:
    def __init__(self, secret: bytes, ttl: timedelta = timedelta(minutes=10)) -> None:
        if len(secret) < 32:
            raise ValueError("Секрет для викликів має бути щонайменше 32 байти")
        self._secret, self.ttl = secret, ttl

    @staticmethod
    def payload(user_id: uuid.UUID, nonce: str, exp: datetime, purpose: str) -> bytes:
        return (f"AI-SED PUET: {purpose}\nuser: {user_id}\nnonce: {nonce}\n"
                f"expires: {exp.isoformat()}\n").encode()

    def issue(self, user_id: uuid.UUID, purpose: str = "key-registration", now: datetime | None = None) -> Challenge:
        now = now or datetime.now(timezone.utc)
        exp = (now + self.ttl).replace(microsecond=0)
        body = {"u": str(user_id), "n": secrets.token_urlsafe(24), "e": exp.isoformat(), "p": purpose}
        raw = json.dumps(body, separators=(",", ":")).encode()
        token = f"{_b64(raw)}.{_b64(hmac.new(self._secret, raw, hashlib.sha256).digest())}"
        return Challenge(token, self.payload(user_id, body["n"], exp, purpose), exp)

    def open(self, token: str, user_id: uuid.UUID, purpose: str = "key-registration",
             now: datetime | None = None) -> Challenge:
        now = now or datetime.now(timezone.utc)
        try:
            raw_b64, mac_b64 = token.split(".", 1)
            raw = _unb64(raw_b64)
            ok = hmac.compare_digest(_unb64(mac_b64), hmac.new(self._secret, raw, hashlib.sha256).digest())
            body = json.loads(raw)
            exp = datetime.fromisoformat(body["e"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ChallengeError("Некоректний виклик") from exc
        if not ok:
            raise ChallengeError("Підпис виклику недійсний")
        if body["u"] != str(user_id) or body["p"] != purpose:
            raise ChallengeError("Виклик видано іншому користувачу або для іншої дії")
        if exp < now:
            raise ChallengeError("Строк дії виклику минув — запросіть новий")
        return Challenge(token, self.payload(user_id, body["n"], exp, purpose), exp)
