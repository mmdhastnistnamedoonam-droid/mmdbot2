# -*- coding: utf-8 -*-
"""ذخیره‌یِ عکسِ دیتابیس روی Cloudflare KV (پلنِ رایگان).

چرا؟ ماشینِ GitHub Actions بعد از هر اجرا پاک می‌شه؛ پس دیتابیسِ ربات باید
یه جایِ بیرونی بمونه. اینجا دیتابیس (با APIِ رسمیِ SQLite) عکس گرفته می‌شه،
gzip + base64 می‌شه و توی KV نوشته می‌شه. دو اسلاتِ a/b به‌نوبت استفاده می‌شن
تا اگه وسطِ نوشتن اتفاقی افتاد، نسخهٔ سالمِ قبلی دست‌نخورده بمونه.

دستورها (روی کامپیوترِ خودت):
    CF_API_TOKEN=... CF_ACCOUNT_ID=... python ops/kv_store.py push bot_data.db
    CF_API_TOKEN=... CF_ACCOUNT_ID=... python ops/kv_store.py pull bot_data.db
    CF_API_TOKEN=... CF_ACCOUNT_ID=... python ops/kv_store.py status
برایِ تست بدونِ کلودفلر: KV_LOCAL_DIR=/some/dir
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import base64
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

from ops.backup_manager import snapshot_db, BackupError

NAMESPACE_TITLE = 'mmd-bot-db'
SLOTS = ('db_a', 'db_b')
META_KEY = 'db_meta'
MAX_VALUE = 24 * 1024 * 1024      # سقفِ KV برایِ هر مقدار ۲۵ مگابایته؛ با حاشیه


class StoreError(Exception):
    pass


# ─────────────────────────── ذخیره‌گاه‌ها ───────────────────────────
class LocalKV:
    """فقط برایِ تست روی یه پوشه."""
    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)

    def get(self, key):
        p = os.path.join(self.path, key)
        if not os.path.isfile(p):
            return None
        with open(p, encoding='utf-8') as f:
            return f.read()

    def put(self, key, text):
        with open(os.path.join(self.path, key), 'w', encoding='utf-8') as f:
            f.write(text)


class CloudflareKV:
    def __init__(self, token, account, namespace_id=None):
        import requests
        self._rq = requests
        self.account = account
        self.headers = {'Authorization': 'Bearer %s' % token}
        self._ns = namespace_id or None

    def _base(self):
        return 'https://api.cloudflare.com/client/v4/accounts/%s/storage/kv/namespaces' % self.account

    def _req(self, method, url, **kw):
        last = None
        for attempt in range(4):
            try:
                r = self._rq.request(method, url, headers=self.headers, timeout=60, **kw)
            except self._rq.RequestException as e:
                last = e
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = 'HTTP %s' % r.status_code
                time.sleep(2 ** attempt)
                continue
            return r
        raise StoreError('ارتباط با Cloudflare برقرار نشد: %s' % last)

    def namespace(self):
        """آیدیِ namespace؛ اگه نبود با اسمِ mmd-bot-db می‌سازه."""
        if self._ns:
            return self._ns
        page = 1
        while True:
            r = self._req('GET', self._base(), params={'per_page': 100, 'page': page})
            if r.status_code != 200:
                raise StoreError('لیستِ KV خوانده نشد (%s). دسترسیِ توکن را چک کن: %s'
                                 % (r.status_code, r.text[:200]))
            items = r.json().get('result') or []
            for it in items:
                if it.get('title') == NAMESPACE_TITLE:
                    self._ns = it['id']
                    return self._ns
            if len(items) < 100:
                break
            page += 1
        r = self._req('POST', self._base(), json={'title': NAMESPACE_TITLE})
        if r.status_code != 200:
            raise StoreError('ساختِ KV نشد (%s): %s' % (r.status_code, r.text[:200]))
        self._ns = r.json()['result']['id']
        return self._ns

    def get(self, key):
        r = self._req('GET', '%s/%s/values/%s' % (self._base(), self.namespace(), key))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise StoreError('خواندنِ %s ناموفق (%s): %s' % (key, r.status_code, r.text[:200]))
        return r.text

    def put(self, key, text):
        r = self._req('PUT', '%s/%s/values/%s' % (self._base(), self.namespace(), key),
                      files={'value': (None, text), 'metadata': (None, '{}')})
        if r.status_code != 200:
            raise StoreError('نوشتنِ %s ناموفق (%s): %s' % (key, r.status_code, r.text[:200]))


def make_store():
    local = os.environ.get('KV_LOCAL_DIR')
    if local:
        return LocalKV(local)
    token = os.environ.get('CF_API_TOKEN')
    account = os.environ.get('CF_ACCOUNT_ID')
    if not token or not account:
        raise StoreError('CF_API_TOKEN و CF_ACCOUNT_ID تنظیم نشده‌اند.')
    return CloudflareKV(token, account, os.environ.get('CF_KV_NAMESPACE_ID'))


# ─────────────────────────── منطقِ عکس‌گیری ───────────────────────────
def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def _encode(path):
    with open(path, 'rb') as f:
        gz = gzip.compress(f.read(), compresslevel=9, mtime=0)
    text = base64.b64encode(gz).decode('ascii')
    if len(text) > MAX_VALUE:
        raise StoreError('دیتابیس برایِ KV بزرگ است (%.1f MB پس از فشرده‌سازی؛ سقف ۲۴MB).'
                         % (len(text) / 1048576))
    return text


def _decode_to(text, dst):
    raw = gzip.decompress(base64.b64decode(text))
    with open(dst, 'wb') as f:
        f.write(raw)


def _check_db(path):
    c = sqlite3.connect(path)
    try:
        ok = c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        n = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    finally:
        c.close()
    return ok and n > 0


def _meta(store):
    t = store.get(META_KEY)
    if not t:
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None


def save_snapshot(db_path, store=None, force=False):
    """عکسِ سالم می‌گیره و (اگه نسبت به آخرین نسخه عوض شده) آپلود می‌کنه.
    برمی‌گرداند: 'uploaded' | 'unchanged'."""
    store = store or make_store()
    tmpdir = tempfile.mkdtemp(prefix='dbsnap_')
    try:
        snap = os.path.join(tmpdir, 'snap.db')
        try:
            res = snapshot_db(db_path, snap)
        except BackupError as e:
            raise StoreError(str(e))
        if not res['ok']:
            raise StoreError('عکسِ دیتابیس سالم نیست (%s)؛ آپلود نشد.' % res['integrity'])
        sha = _sha(snap)
        meta = _meta(store)
        if meta and meta.get('sha') == sha and not force:
            return 'unchanged'
        slot = SLOTS[1] if (meta and meta.get('slot') == SLOTS[0]) else SLOTS[0]
        store.put(slot, _encode(snap))
        store.put(META_KEY, json.dumps({'slot': slot, 'sha': sha, 'ts': int(time.time()),
                                        'size': res['size']}))
        return 'uploaded'
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def restore_snapshot(dst_path, store=None):
    """آخرین نسخه را برمی‌گرداند. 'none' = هنوز هیچ نسخه‌ای ذخیره نشده (اولین اجرا).
    اگه نسخه هست ولی خونده نشد → StoreError (تا دیتابیسِ خالی جایِ نسخهٔ خوب ننشینه)."""
    store = store or make_store()
    meta = _meta(store)
    if not meta:
        return 'none'
    order = [meta.get('slot')] + [s for s in SLOTS if s != meta.get('slot')]
    errors = []
    for slot in order:
        if slot not in SLOTS:
            continue
        tmpdir = tempfile.mkdtemp(prefix='dbrest_')
        try:
            text = store.get(slot)
            if not text:
                errors.append('%s: خالی' % slot)
                continue
            tmp = os.path.join(tmpdir, 'r.db')
            _decode_to(text, tmp)
            if not _check_db(tmp):
                errors.append('%s: خراب' % slot)
                continue
            for ext in ('', '-wal', '-shm'):
                try:
                    os.remove(dst_path + ext)
                except OSError:
                    pass
            shutil.move(tmp, dst_path)
            return 'restored' if slot == meta.get('slot') else 'restored-fallback'
        except StoreError:
            raise
        except Exception as e:
            errors.append('%s: %s' % (slot, e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    raise StoreError('نسخهٔ ذخیره‌شده خوانده نشد: ' + ' | '.join(errors))


def _cli(argv):
    if len(argv) < 2 or argv[1] not in ('push', 'pull', 'status'):
        print(__doc__)
        return 2
    store = make_store()
    cmd = argv[1]
    if cmd == 'status':
        m = _meta(store)
        if not m:
            print('هنوز نسخه‌ای ذخیره نشده.')
        else:
            print('آخرین نسخه: اسلات %s، %.2f MB، %s' % (
                m['slot'], m['size'] / 1048576,
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(m['ts']))))
        return 0
    if len(argv) < 3:
        print('مسیرِ فایل را بده.')
        return 2
    if cmd == 'push':
        print('⚠️ مطمئن شو ربات روی GitHub روشن نیست (وگرنه نسخهٔ خودش را رویِ این می‌نویسد).')
        print('نتیجه:', save_snapshot(argv[2], store, force=True))
    else:
        print('نتیجه:', restore_snapshot(argv[2], store))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(_cli(sys.argv))
    except StoreError as e:
        print('خطا:', e)
        sys.exit(1)
