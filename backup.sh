#!/usr/bin/env bash
# Бэкап базы. В crontab: 0 */6 * * * /opt/playerok_bot/backup.sh
#
# Используется sqlite3 .backup, а НЕ cp: при копировании работающей базы
# с WAL можно получить битый файл.
set -euo pipefail

DB="${DB_PATH:-/opt/playerok_bot/data/store.db}"
DEST="${BACKUP_DIR:-/opt/playerok_bot/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/store-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"
gzip -9 "$OUT"

find "$DEST" -name 'store-*.db.gz' -mtime "+$KEEP_DAYS" -delete

echo "OK: $OUT.gz"

# ВНИМАНИЕ: база бесполезна без FERNET_KEY из .env.
# Храните ключ ОТДЕЛЬНО от бэкапов (менеджер паролей), иначе:
#  - ключ рядом с базой = шифрование не защищает ни от чего;
#  - ключ потерян = аккаунты не расшифровать.
