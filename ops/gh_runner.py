# -*- coding: utf-8 -*-
"""اجراکنندهٔ ربات روی GitHub Actions.

مراحل: ۱) بازیابیِ دیتابیس از Cloudflare KV  ۲) اجرایِ main.py  ۳) هر چند دقیقه
عکس‌گیریِ دیتابیس  ۴) بعد از RUN_MINUTES توقفِ تمیز + عکسِ نهایی (اجرایِ بعدی از
همین‌جا ادامه می‌دهد).

لاگِ خودِ ربات (آیدی/نامِ کاربرها) داخلِ فایلِ bot.log می‌ماند و روی صفحهٔ
Actions چاپ نمی‌شود (چون ریپوی عمومی لاگ‌ها را همه می‌توانند ببینند).
اگه برایِ عیب‌یابی لازم داشتی: متغیرِ DEBUG_LOGS=1 (Repository variable).
"""
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _ROOT)

import os
import re
import signal
import subprocess
import sys
import time

from ops import kv_store

DB_PATH = _os.path.join(_ROOT, "bot_data.db")
RUN_SECONDS = float(os.environ.get('RUN_MINUTES', '340')) * 60
SNAP_EVERY = float(os.environ.get('SNAPSHOT_SECONDS', '300'))
DEBUG = os.environ.get('DEBUG_LOGS', '').strip() in ('1', 'true', 'yes')
MAX_RESTARTS_PER_HOUR = 5

_stop = False


def log(msg):
    print('[runner] ' + msg, flush=True)


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    log('سیگنالِ توقف دریافت شد (%s)؛ در حالِ بستنِ تمیز...' % signum)


def _check_env():
    tok = os.environ.get('BOT_TOKEN', '').strip()
    if not re.match(r'^[0-9]+:[A-Za-z0-9_-]{30,}$', tok):
        log('❌ سکرتِ BOT_TOKEN نیست یا شکلش درست نیست.')
        return False
    if not os.environ.get('KV_LOCAL_DIR'):
        if not os.environ.get('CF_API_TOKEN') or not os.environ.get('CF_ACCOUNT_ID'):
            log('❌ سکرت‌هایِ CLOUDFLARE_API_TOKEN و CLOUDFLARE_ACCOUNT_ID تنظیم نشده‌اند.')
            return False
    if not os.environ.get('OWNER_ID', '').strip().isdigit():
        log('⚠️ OWNER_ID تنظیم نشده؛ ربات بدونِ مالک بالا می‌آید.')
    return True


def _restore():
    if os.path.isfile(DB_PATH):
        log('دیتابیسِ محلی موجود است؛ بازیابی لازم نیست.')
        return True
    for attempt in range(1, 4):
        try:
            res = kv_store.restore_snapshot(DB_PATH)
            if res == 'none':
                log('هنوز نسخه‌ای ذخیره نشده؛ با دیتابیسِ خالی شروع می‌شود (اولین اجرا).')
            else:
                log('دیتابیس بازیابی شد (%s).' % res)
            return True
        except kv_store.StoreError as e:
            log('بازیابی ناموفق (تلاش %d/3): %s' % (attempt, e))
            time.sleep(5 * attempt)
    log('❌ دیتابیس بازیابی نشد؛ برایِ اینکه نسخهٔ سالمِ قبلی بازنویسی نشود، ربات روشن نمی‌شود.')
    return False


def _snapshot(tag):
    try:
        res = kv_store.save_snapshot(DB_PATH)
        if res == 'uploaded':
            log('عکسِ دیتابیس ذخیره شد (%s).' % tag)
    except Exception as e:  # noqa
        log('⚠️ عکس‌گیری ناموفق (%s): %s' % (tag, e))
        return False
    return True


def _start_child():
    env = {k: v for k, v in os.environ.items()
           if k not in ('CF_API_TOKEN', 'CF_ACCOUNT_ID', 'CF_KV_NAMESPACE_ID')}
    env['PYTHONUNBUFFERED'] = '1'
    if DEBUG:
        out = None
    else:
        out = open(os.path.join(_ROOT, 'bot.log'), 'ab')
    p = subprocess.Popen([sys.executable, 'main.py'], cwd=_ROOT, env=env,
                         stdout=out, stderr=subprocess.STDOUT if out else None)
    log('ربات روشن شد (pid %d).' % p.pid)
    return p


def _stop_child(p):
    if p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(25)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    os.chdir(_ROOT)
    if not _check_env() or not _restore():
        return 1

    deadline = time.time() + RUN_SECONDS
    next_snap = time.time() + SNAP_EVERY
    restarts = []
    child = _start_child()
    code = 0
    while not _stop and time.time() < deadline:
        time.sleep(1)
        if child.poll() is not None:
            now = time.time()
            restarts = [t for t in restarts if now - t < 3600]
            log('⚠️ ربات با کدِ %s بسته شد.' % child.returncode)
            if not DEBUG:
                log('   (لاگِ ربات مخفی است؛ برایِ دیدنش متغیرِ DEBUG_LOGS=1 را بگذار.)')
            if len(restarts) >= MAX_RESTARTS_PER_HOUR:
                log('❌ بیش از حد کرش کرد؛ متوقف می‌شوم.')
                code = 1
                break
            restarts.append(now)
            time.sleep(10)
            child = _start_child()
        if time.time() >= next_snap:
            _snapshot('دوره‌ای')
            next_snap = time.time() + SNAP_EVERY

    log('پایانِ نوبت؛ توقفِ ربات...')
    _stop_child(child)
    for _ in range(3):
        if _snapshot('نهایی'):
            break
        time.sleep(3)
    log('تمام. نوبتِ بعدی ادامه می‌دهد.')
    return code


if __name__ == '__main__':
    sys.exit(main())
