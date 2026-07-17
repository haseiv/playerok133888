# Деплой

## Какой хостинг подойдёт

Боту нужен **постоянно работающий процесс** и **диск, который переживает
перезапуск**. Отсюда:

| Вариант | Годится | Комментарий |
|---|---|---|
| VPS (Timeweb, Selectel, Aeza, Hetzner…) | ✅ | Самое простое. Хватит 1 vCPU / 1 ГБ / 10 ГБ |
| Docker-хостинг, свой сервер | ✅ | `docker compose up -d` |
| Shared-хостинг с FTP/cPanel | ❌ | Нет долгоживущих процессов |
| Serverless (Lambda, Cloud Functions) | ⚠️ | Только с переписыванием на webhook + внешняя БД. Файловая система там временная — SQLite умрёт |
| Heroku-подобные с эфемерным диском | ⚠️ | То же: база сотрётся при рестарте |

Минимум ресурсов: бот почти ничего не ест, упрётесь разве что в диск под бэкапы.

---

## Вариант A. Docker (рекомендую)

```bash
# на сервере
git clone <ваш_репозиторий> /opt/playerok_bot && cd /opt/playerok_bot
cp .env.example .env
nano .env                                      # вписать значения
docker compose up -d --build
docker compose logs -f
```

Обновление кода:
```bash
git pull && docker compose up -d --build
```
База в `./data` — переживает пересборку.

## Вариант B. systemd (без Docker)

```bash
adduser --system --group botuser
mkdir -p /opt/playerok_bot && cd /opt/playerok_bot
# скопировать сюда файлы проекта
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
mkdir -p data && chown -R botuser:botuser /opt/playerok_bot
chmod 600 .env

cp playerok-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now playerok-bot
journalctl -u playerok-bot -f
```

---

## Обязательные шаги после запуска

1. **Права на .env** — `chmod 600 .env`. Там токен бота и ключ шифрования.
2. **Бэкапы** — `chmod +x backup.sh`, в cron:
   ```
   0 */6 * * * /opt/playerok_bot/backup.sh >> /var/log/bot-backup.log 2>&1
   ```
   Проверьте, что бэкап **восстанавливается**, а не просто создаётся.
3. **Скопируйте ключ шифрования в менеджер паролей.** Он в `data/.fernet_key`
   (создаётся при первом запуске) или в переменной `FERNET_KEY`. Потеряете —
   потеряете весь склад: расшифровать будет нечем. Копию держите отдельно
   от сервера.
4. **Один экземпляр бота.** Two polling-процесса с одним токеном будут драться
   за апдейты и выдавать заказы дважды. Не запускайте Docker и systemd
   одновременно, не оставляйте бота на локальной машине после деплоя.
5. **Файрвол** — входящие боту не нужны вообще (long polling исходящий):
   ```bash
   ufw allow OpenSSH && ufw enable
   ```
6. **SSH по ключу**, пароль отключить. На сервере лежат чужие аккаунты.

## Проверка, что всё живо

```bash
docker compose ps            # или: systemctl status playerok-bot
```
Напишите боту `/start` — админ должен увидеть панель продавца.
Если тишина: `docker compose logs --tail=50` / `journalctl -u playerok-bot -n 50`.

## Частые грабли

- **Бот молчит, в логах `Unauthorized`** — неверный `BOT_TOKEN`.
- **`Не удалось расшифровать данные`** — `FERNET_KEY` не тот, которым шифровали.
  Восстановите старый ключ, новый ключ старую базу не откроет.
- **`TerminatedByOtherGetUpdates`** — где-то работает второй экземпляр.
- **Склад пропал после `docker compose down -v`** — флаг `-v` сносит тома.
  Никогда не используйте его здесь.
- **Часовой пояс в логах не тот** — правьте `TZ` в Dockerfile.
- **`UnauthorizedError` от Playerok** — протухли cookies. Заново снимите
  `token` и `__ddg3` из браузера, обновите `.env`, перезапустите. Бот сам
  напишет вам в Telegram, когда это случится.
- **`BotCheckDetectedException`** — антибот. Чаще всего `PLAYEROK_USER_AGENT`
  не совпадает с браузером, где вы логинились.
- **Сборка падает на `git+https://...`** — в образе нет git. Он ставится в
  Dockerfile; при локальной установке без Docker поставьте git сам.
