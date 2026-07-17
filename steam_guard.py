"""Парсинг maFile (Steam Desktop Authenticator) и генерация кодов Steam Guard.

Steam использует TOTP (RFC 6238) с шагом 30 секунд и SHA-1, но кодирует
результат не в 6 цифр, а в 5 символов своего алфавита.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import struct
import time
from dataclasses import dataclass

# Алфавит Steam: без 0/1/A/E/I/L/O/S/U/Z — чтобы не путать похожие символы.
STEAM_ALPHABET = "23456789BCDFGHJKMNPQRTVWXY"
CODE_LENGTH = 5
TIME_STEP = 30


class MaFileError(ValueError):
    """maFile битый, не тот формат или без shared_secret."""


@dataclass(frozen=True)
class MaFile:
    account_name: str
    shared_secret: str
    identity_secret: str | None
    steam_id: str | None
    revocation_code: str | None
    raw: str  # исходный JSON — отдаём покупателю как есть

    @classmethod
    def parse(cls, content: str | bytes) -> "MaFile":
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8-sig")
            except UnicodeDecodeError as e:
                raise MaFileError("Файл не в UTF-8. Это точно maFile?") from e

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise MaFileError(f"Не валидный JSON: {e}") from e

        if not isinstance(data, dict):
            raise MaFileError("Ожидался JSON-объект")

        shared = data.get("shared_secret")
        if not shared:
            raise MaFileError("В файле нет поля shared_secret")

        # Проверяем, что секрет реально рабочий, а не строка-заглушка
        try:
            generate_code(shared)
        except Exception as e:
            raise MaFileError(f"shared_secret некорректный: {e}") from e

        session = data.get("Session") or {}
        steam_id = data.get("steam_id") or session.get("SteamID")

        return cls(
            account_name=data.get("account_name") or session.get("Username") or "unknown",
            shared_secret=shared,
            identity_secret=data.get("identity_secret"),
            steam_id=str(steam_id) if steam_id else None,
            revocation_code=data.get("revocation_code"),
            raw=content,
        )

    def code(self) -> str:
        return generate_code(self.shared_secret)

    def seconds_left(self) -> int:
        return seconds_left()


def generate_code(shared_secret: str, timestamp: float | None = None) -> str:
    """Генерирует 5-символьный код Steam Guard."""
    if timestamp is None:
        timestamp = time.time()

    try:
        key = base64.b64decode(shared_secret, validate=True)
    except (binascii.Error, ValueError) as e:
        raise MaFileError("shared_secret не является корректным base64") from e

    if not key:
        raise MaFileError("shared_secret пустой")

    counter = int(timestamp) // TIME_STEP
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()

    # Динамическое усечение по RFC 4226
    offset = digest[19] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF

    code = []
    for _ in range(CODE_LENGTH):
        code.append(STEAM_ALPHABET[value % len(STEAM_ALPHABET)])
        value //= len(STEAM_ALPHABET)
    return "".join(code)


def seconds_left(timestamp: float | None = None) -> int:
    """Сколько секунд текущий код ещё живёт."""
    if timestamp is None:
        timestamp = time.time()
    return TIME_STEP - int(timestamp) % TIME_STEP
