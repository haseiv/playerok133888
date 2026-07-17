"""Шифрование maFile и паролей перед записью в БД.

maFile содержит shared_secret — по сути ключ от Steam-аккаунта. Хранить его
в базе открытым текстом нельзя: утёк файл store.db — утёк весь склад.

Ключ берётся так:
  1. Переменная окружения FERNET_KEY, если задана.
  2. Иначе — файл рядом с базой. Нет файла — генерируется при первом запуске.

Второй вариант удобнее (настраивать нечего), но защищает слабее: ключ лежит
рядом с базой, и утечка всего каталога data/ вскроет и то, и другое. Он спасёт
от случайно опубликованной базы, но не от доступа к серверу.
Нужна защита посерьёзнее — задайте FERNET_KEY переменной и храните отдельно.
"""

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from config import cfg

log = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _key_path() -> str:
    return os.path.join(os.path.dirname(cfg.db_path) or ".", ".fernet_key")


def _load_or_create_key() -> bytes:
    if cfg.fernet_key:
        return cfg.fernet_key.encode()

    path = _key_path()
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = Fernet.generate_key()
    # Пишем до 0600: между созданием и chmod файл не должен быть читаем всем.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    log.warning(
        "Ключ шифрования создан: %s\n"
        "СДЕЛАЙТЕ КОПИЮ ЭТОГО ФАЙЛА. Потеряете — склад аккаунтов не "
        "расшифровать ничем, включая бэкапы базы.", path
    )
    return key


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt(plain: str) -> bytes:
    return _f().encrypt(plain.encode("utf-8"))


def decrypt(blob: bytes) -> str:
    try:
        return _f().decrypt(blob).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "Не удалось расшифровать данные: ключ не тот, которым шифровали "
            f"базу. Проверьте FERNET_KEY и файл {_key_path()}."
        ) from e


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
