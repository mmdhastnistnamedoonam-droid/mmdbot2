#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  install_bot.sh — نصبِ رباتِ تلگرامِ مستقل (بدونِ سایت و اپ) به‌صورتِ سرویسِ systemd
#
#  اجرا (با root، داخلِ همین پوشه):
#     bash install_bot.sh
#  اگه توکنِ ربات و آیدیِ مالک رو نداده باشی، همین‌جا ازت می‌پرسه. یا از قبل بده:
#     BOT_TOKEN=123:ABC... OWNER_ID=123456789 bash install_bot.sh
#  (آیدیِ عددیِ خودت رو از @userinfobot بگیر؛ توکن از @BotFather)
#  اگه دیتابیسِ قبلی رو می‌خوای، فایلِ bot_data.db رو قبلِ اجرا کنارِ main.py بذار.
# ══════════════════════════════════════════════════════════════════════════════
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="amirmmd-botonly"
c_g=$'\033[1;32m'; c_y=$'\033[1;33m'; c_r=$'\033[1;31m'; c_b=$'\033[1;34m'; c_0=$'\033[0m'
step() { printf '\n%s════ %s ════%s\n' "$c_b" "$*" "$c_0"; }
ok()   { printf '%s  ✔ %s%s\n' "$c_g" "$*" "$c_0"; }
warn() { printf '%s  ! %s%s\n' "$c_y" "$*" "$c_0"; }
die()  { printf '%s  ✘ %s%s\n' "$c_r" "$*" "$c_0" >&2; exit 1; }
ask()  { local v; if [ -r /dev/tty ]; then read -r -p "$1" v </dev/tty || true; fi; printf '%s' "${v:-}"; }

[ "$(id -u)" -eq 0 ] || [ -n "${INSTALL_TEST:-}" ] || die "با root اجرا کن:  sudo bash install_bot.sh"
[ -f "$DIR/main.py" ] && [ -d "$DIR/bot" ] || die "این اسکریپت باید کنارِ main.py و پوشه‌یِ bot اجرا بشه."

step "بسته‌هایِ سیستم"
command -v apt-get >/dev/null 2>&1 || die "فقط Ubuntu/Debian (apt-get پیدا نشد)."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null 2>&1 || warn "apt update خطا داد؛ ادامه می‌دیم."
apt-get install -y python3 python3-venv python3-pip build-essential curl ca-certificates >/dev/null 2>&1 || warn "نصبِ بعضی بسته‌ها خطا داد."
ok "python3 + venv"

step "توکنِ ربات"
TOKEN="${BOT_TOKEN:-}"
[ -n "$TOKEN" ] || { [ -s "$DIR/.bot_token" ] && TOKEN="$(tr -d '[:space:]' < "$DIR/.bot_token")"; }
[ -n "$TOKEN" ] || TOKEN="$(ask '   توکنِ ربات (از @BotFather): ')"
[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "توکن معتبر نیست. شکلِ درست:  123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
printf '%s\n' "$TOKEN" > "$DIR/.bot_token"; chmod 600 "$DIR/.bot_token"
RES="$(curl -s -m 10 "https://api.telegram.org/bot$TOKEN/getMe" 2>/dev/null || true)"
if echo "$RES" | grep -q '"ok":true'; then ok "توکن تأیید شد: @$(echo "$RES" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
elif [ -z "$RES" ]; then warn "از این سرور به تلگرام وصل نشدم؛ توکن تأیید نشد. اگه تلگرام فیلتره، ربات کار نمی‌کنه."
else warn "تلگرام توکن رو قبول نکرد (۴۰۱). توکن رو چک کن."; fi

step "مالکِ ربات"
OWNER="${OWNER_ID:-}"
[ -n "$OWNER" ] || { [ -s "$DIR/.owner_id" ] && OWNER="$(tr -d '[:space:]' < "$DIR/.owner_id")"; }
[ -n "$OWNER" ] || OWNER="$(ask '   آیدیِ عددیِ تلگرامِ مالک (از @userinfobot): ')"
[[ "$OWNER" =~ ^[0-9]+$ ]] || die "آیدیِ مالک باید فقط عدد باشه."
printf '%s\n' "$OWNER" > "$DIR/.owner_id"; ok "مالک: $OWNER"

step "محیطِ پایتون"
[ -x "$DIR/venv/bin/python" ] && "$DIR/venv/bin/python" -c 'import sys' >/dev/null 2>&1 || { rm -rf "$DIR/venv"; python3 -m venv "$DIR/venv" || die "ساختِ venv نشد"; }
"$DIR/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt" && ok "پکیج‌ها نصب شد" || warn "نصبِ پکیج‌ها خطا داد (pypi باز نیست؟ آینه:  PIP_INDEX_URL=... bash install_bot.sh)"
MISSING="$("$DIR/venv/bin/python" - <<'PY'
import importlib.util
print(" ".join(p for m, p in {"telebot": "pyTelegramBotAPI", "apscheduler": "APScheduler", "openpyxl": "openpyxl"}.items() if importlib.util.find_spec(m) is None))
PY
)" || die "بررسیِ پکیج‌ها خطا داد."
[ -z "$MISSING" ] || die "این پکیج‌ها نصب نشدن: $MISSING"

if [ -f "$DIR/bot_data.db" ]; then
  "$DIR/venv/bin/python" - "$DIR/bot_data.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
print("   bot_data.db:", "سالم" if c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else "⚠ مشکل دارد")
PY
else warn "bot_data.db نیست؛ یه دیتابیسِ خالی ساخته می‌شه."; fi

step "سرویس"
if systemctl is-active --quiet amirmmd-bot 2>/dev/null; then
  warn "سرویسِ amirmmd-bot (نسخه‌یِ کاملِ پروژه) روشنه. اگه با همین توکن کار می‌کنه، همین الان یکی‌شون رو خاموش کن (تلگرام دو نمونه‌یِ هم‌زمان رو قبول نمی‌کنه):  systemctl stop amirmmd-bot"
fi
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Telegram Bot (standalone)
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload; systemctl enable "$SERVICE" >/dev/null 2>&1; systemctl restart "$SERVICE"; sleep 6
if [ "$(systemctl is-active "$SERVICE")" = "active" ]; then ok "ربات روشنه"
else warn "ربات روشن نموند. آخرین لاگ‌ها:"; journalctl -u "$SERVICE" -n 25 --no-pager 2>&1 | tail -25; die "سرویس بالا نیومد (لاگ بالا)."; fi

cat <<MSG

${c_g}✅ رباتِ مستقل راه افتاد.${c_0}
   • برایِ امتحان: توی تلگرام به ربات /start بفرست (با اکانتِ مالک منویِ مدیریت رو می‌بینی).
   • لاگِ زنده:  journalctl -u $SERVICE -f
   • ریستارت:   systemctl restart $SERVICE          • توقف:  systemctl stop $SERVICE
MSG
