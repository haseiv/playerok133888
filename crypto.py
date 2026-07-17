"""Шифрование maFile перед сохранением в БД.

maFile содержит shared_secret и identity_secret — по сути ключи от аккаунта.
Хранить их в БД в открытом виде нельзя: слитая база = слитые все аккаунты.
Ключ Fernet лежит в .env, база — на диске; компрометация одного файла
без второго бесполезна.
"""

from cryptography.fernet import Fernet, InvalidToken

from config import cfg

_fernet: Fernet | None = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(cfg.fernet_key.encode())
    return _fernet


def encrypt(plain: str) -> bytes:
    return _f().encrypt(plain.encode("utf-8"))


def decrypt(blob: bytes) -> str:
    try:
        return _f().decrypt(blob).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "Не удалось расшифровать данные. Скорее всего FERNET_KEY "
            "не совпадает с тем, которым шифровали базу."
        ) from e


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
