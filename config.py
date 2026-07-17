import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _ids(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(",", " ").split() if x.strip().isdigit()]


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=lambda: _ids(os.getenv("ADMIN_IDS", "")))
    db_path: str = os.getenv("DB_PATH", "data/store.db")
    # Ключ шифрования maFile в БД. Сгенерировать: python -m crypto_keygen
    fernet_key: str = os.getenv("FERNET_KEY", "")
    # Playerok: авторизация по cookies живой сессии, а не по токену API.
    # Формат: __ddg3=ЗНАЧЕНИЕ;token=ЗНАЧЕНИЕ
    playerok_cookies: str = os.getenv("PLAYEROK_COOKIES", "")
    playerok_user_agent: str = os.getenv("PLAYEROK_USER_AGENT", "")
    # Если IP сервера не нравится защите Playerok. Формат:
    # http://user:pass@host:port
    playerok_proxy: str = os.getenv("PLAYEROK_PROXY", "")
    # После аренды аккаунт уходит в maintenance и ждёт вашей проверки
    # (сменить пароль и т.п.), а не сдаётся сразу следующему.
    rental_maintenance: bool = os.getenv("RENTAL_MAINTENANCE", "false").lower() in (
        "1", "true", "yes", "да"
    )
    bot_username: str = os.getenv("BOT_USERNAME", "")

    def validate(self) -> None:
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN не задан в .env")
        if not self.fernet_key:
            raise RuntimeError("FERNET_KEY не задан в .env (см. README)")
        if not self.admin_ids:
            raise RuntimeError("ADMIN_IDS не задан в .env")
        # Cookies без User-Agent = мгновенный разлогин и антибот.
        if self.playerok_cookies and not self.playerok_user_agent:
            raise RuntimeError(
                "PLAYEROK_COOKIES задан, но PLAYEROK_USER_AGENT пуст. "
                "Нужен тот же User-Agent, что и в браузере, где вы вошли."
            )

    @property
    def playerok_enabled(self) -> bool:
        return bool(self.playerok_cookies and self.playerok_user_agent)


cfg = Config()
