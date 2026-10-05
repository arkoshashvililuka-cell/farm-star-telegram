"""Farm Stars — Telegram Mini App backend (stdlib only).

Changes vs. the previous version:
  * every API call runs in ONE serialized DB transaction -> no double payouts / races
  * /api/game/lucky added (the newest index.html calls it)
  * roulette payouts are now stake multipliers (before: fixed prizes -> +88% player edge at stake 10)
  * higher/lower pays by real win probability (before: flat x2 -> "pick the 95% side" exploit)
  * harvest on an empty field no longer works
  * rocket/start no longer crashes after charging the stake; active rounds can't be overwritten
  * mines use the real combinatorial multiplier with a house edge, capped
  * crash point / outcomes use SystemRandom (not predictable Mersenne Twister)
  * Telegram messages are sent AFTER commit, never while holding the DB lock
"""
import hashlib, hmac, json, math, os, random, secrets, sqlite3, threading, time
import urllib.parse, urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
WEB_APP_URL = os.getenv('WEB_APP_URL', 'https://farm-star-telegram.onrender.com/').strip()
PORT = int(os.getenv('PORT', '10000'))
DB_PATH = os.getenv('DB_PATH', '/data/farmstar.db')
MIN_STAKE, MAX_STAKE = 10, int(os.getenv('MAX_STAKE', '1000'))
SHOP_AMOUNTS = [10, 25, 50, 100, 250, 500, 1000]
EDGE = 0.95                       # target return-to-player for every game
REF_BONUS, REF_MAX, REF_SECONDS = 10, 5, 60
MAX_MULT = 100.0
ADMIN_IDS = {int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x.isdigit()}
SUPPORT = os.getenv('SUPPORT_CONTACT', '').strip()      # e.g. @your_support or an email
RATE_PER_SEC, RATE_BURST = 12.0, 30.0
BACKUP_EVERY, BACKUP_KEEP = 6 * 3600, 5

# Lucky Spin sectors (multiplier, probability). RTP = sum(m*p) = 0.962
LUCKY = [(0, .58), (1.2, .16), (1.5, .12), (2, .07), (5, .05), (10, .02)]
# Roulette sectors (multiplier, probability). RTP = 0.94
ROULETTE = [(0, .42), (.5, .30), (1.5, .16), (2.5, .06), (5, .04), (10, .02)]
DAILY = [(5, 40), (15, 25), (25, 15), (50, 10), (75, 7), (100, 3)]

rng = random.SystemRandom()
LOCK = threading.RLock()
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
if not BOT_TOKEN:
    print('WARNING: BOT_TOKEN is not set')


class ApiError(Exception):
    pass


# ───────────────────────── database ─────────────────────────
def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c


SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
 stars INTEGER NOT NULL DEFAULT 50, wheat INTEGER NOT NULL DEFAULT 0,
 farm_level INTEGER NOT NULL DEFAULT 1, energy REAL NOT NULL DEFAULT 10, energy_ts REAL NOT NULL DEFAULT 0,
 crop_stage TEXT NOT NULL DEFAULT 'empty', crop_planted REAL NOT NULL DEFAULT 0, crop_ready REAL NOT NULL DEFAULT 0,
 daily_next REAL NOT NULL DEFAULT 0, referrer_id INTEGER, referrals INTEGER NOT NULL DEFAULT 0,
 referral_code TEXT UNIQUE, referral_pending INTEGER NOT NULL DEFAULT 0, referral_qualified INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, last_seen REAL NOT NULL DEFAULT 0, username_display TEXT, photo_url TEXT);
CREATE TABLE IF NOT EXISTS payments(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
 payload TEXT UNIQUE NOT NULL, charge_id TEXT UNIQUE, credited INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS games(
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL,
 stake INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS results(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, type TEXT NOT NULL,
 value TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_results ON results(user_id, type, id);
CREATE TABLE IF NOT EXISTS ledger(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, delta INTEGER NOT NULL,
 reason TEXT NOT NULL, balance INTEGER, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ledger ON ledger(user_id, id);
CREATE TABLE IF NOT EXISTS referrals(
 id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL, referred_id INTEGER UNIQUE NOT NULL,
 started_at REAL NOT NULL, qualified_at REAL, paid INTEGER NOT NULL DEFAULT 0);
'''


def init_db():
    c = connect()
    c.executescript(SCHEMA)
    cols = {r['name'] for r in c.execute('PRAGMA table_info(users)')}
    if 'photo_url' not in cols:
        c.execute('ALTER TABLE users ADD COLUMN photo_url TEXT')
    pcols = {r['name'] for r in c.execute('PRAGMA table_info(payments)')}
    if 'refunded' not in pcols:
        c.execute('ALTER TABLE payments ADD COLUMN refunded INTEGER NOT NULL DEFAULT 0')
    c.commit()
    c.close()


init_db()


@contextmanager
def transaction():
    """Serialized transaction: one API call at a time, all-or-nothing."""
    with LOCK:
        c = connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()


# ───────────────────────── telegram ─────────────────────────
def tg(method, payload):
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN missing')
    data = urllib.parse.urlencode({k: v for k, v in payload.items() if v is not None}).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}', data=data)
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def send(uid, text, markup=None):
    try:
        return tg('sendMessage', {'chat_id': uid, 'text': text,
                                  'reply_markup': json.dumps(markup) if markup else None})
    except Exception as e:
        print('send error', e)


def web_markup():
    return {'inline_keyboard': [
        [{'text': '🌟 Открыть ботру', 'web_app': {'url': WEB_APP_URL}}],
        [{'text': '🛒 Пополнить Farm Stars', 'callback_data': 'shop'}],
        [{'text': '👥 Рефералы', 'callback_data': 'ref'}]]}


_bot_username = None


def bot_username():
    global _bot_username
    if not _bot_username:
        try:
            _bot_username = tg('getMe', {})['result']['username']
        except Exception:
            return 'Farme_star_bot'
    return _bot_username


def parse_init(raw):
    if not raw or not BOT_TOKEN:
        raise ApiError('Telegram authorization required')
    vals = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    got = vals.pop('hash', None)
    if not got:
        raise ApiError('Bad initData')
    check = '\n'.join(f'{k}={vals[k]}' for k in sorted(vals))
    secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        raise ApiError('Bad initData signature')
    if time.time() - int(vals.get('auth_date', '0')) > 86400:
        raise ApiError('initData expired')
    return json.loads(vals['user'])


# ───────────────────────── users / state ─────────────────────────
def ensure_user(c, u, ref=None):
    uid, now = int(u['id']), time.time()
    row = c.execute('SELECT id FROM users WHERE id=?', (uid,)).fetchone()
    if row:
        c.execute('UPDATE users SET username=?,first_name=?,last_seen=?,photo_url=? WHERE id=?',
                  (u.get('username'), u.get('first_name', ''), now, u.get('photo_url'), uid))
        return uid
    code = 'FS' + secrets.token_urlsafe(6).replace('-', '').replace('_', '').upper()
    refid = int(ref) if ref and str(ref).isdigit() and int(ref) != uid else None
    if refid and not c.execute('SELECT 1 FROM users WHERE id=?', (refid,)).fetchone():
        refid = None
    c.execute('INSERT INTO users(id,username,first_name,referrer_id,referral_code,created_at,last_seen,photo_url) '
              'VALUES(?,?,?,?,?,?,?,?)',
              (uid, u.get('username'), u.get('first_name', ''), refid, code, now, now, u.get('photo_url')))
    if refid:
        c.execute('INSERT OR IGNORE INTO referrals(referrer_id,referred_id,started_at) VALUES(?,?,?)', (refid, uid, now))
        c.execute('UPDATE users SET referral_pending=1 WHERE id=?', (uid,))
    return uid


def get_user(c, uid):
    r = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not r:
        raise ApiError('User not found')
    return r


def energy_now(r, now=None):
    now = now or time.time()
    mx = min(100, 10 + r['farm_level'] - 1)
    ts = float(r['energy_ts'] or now)
    return min(mx, float(r['energy']) + (now - ts) / 60), mx


def upgrade_cost(level):
    return max(20, int(40 * level ** 1.25))


def state(c, uid):
    r = get_user(c, uid)
    cur, mx = energy_now(r)
    return {'user': {'id': r['id'], 'username': r['username'],
                     'name': r['first_name'] or r['username'] or 'Игрок', 'photo_url': r['photo_url']},
            'stars': r['stars'], 'referrals': r['referrals'], 'daily_next': r['daily_next'],
            'farm': {'level': r['farm_level'], 'wheat': r['wheat'], 'energy': int(cur), 'energy_max': mx,
                     'crop_stage': r['crop_stage'], 'ready_at': r['crop_ready'],
                     'upgrade_cost': upgrade_cost(r['farm_level'])}}


def log_ledger(c, uid, delta, reason):
    bal = c.execute('SELECT stars FROM users WHERE id=?', (uid,)).fetchone()['stars']
    c.execute('INSERT INTO ledger(user_id,delta,reason,balance,created_at) VALUES(?,?,?,?,?)',
              (uid, delta, reason, bal, time.time()))


def spend(c, uid, n, reason='bet'):
    cur = c.execute('UPDATE users SET stars=stars-? WHERE id=? AND stars>=?', (n, uid, n))
    if cur.rowcount != 1:
        have = get_user(c, uid)['stars']
        raise ApiError(f'Недостаточно Farm Stars. Не хватает {n - have} ⭐')
    log_ledger(c, uid, -n, reason)


def earn(c, uid, n, reason='win'):
    if n:
        c.execute('UPDATE users SET stars=stars+? WHERE id=?', (int(n), uid))
        log_ledger(c, uid, int(n), reason)


def record(c, uid, typ, value):
    c.execute('INSERT INTO results(user_id,type,value,created_at) VALUES(?,?,?,?)', (uid, typ, str(value), time.time()))


def history(c, uid, typ, cast=int, n=10):
    return [cast(x['value']) for x in c.execute(
        'SELECT value FROM results WHERE user_id=? AND type=? ORDER BY id DESC LIMIT ?', (uid, typ, n))]


def stake_of(data):
    try:
        s = int(data.get('stake', MIN_STAKE))
    except (TypeError, ValueError):
        raise ApiError('Некорректная ставка')
    if s < MIN_STAKE or s % 10 or s > MAX_STAKE:
        raise ApiError(f'Ставка от {MIN_STAKE} до {MAX_STAKE}, шаг 10')
    return s


def pick(table):
    x, acc = rng.random(), 0.0
    for value, p in table:
        acc += p
        if x < acc:
            return value
    return table[-1][0]


# ───────────────────────── API handlers: (c, uid, data, now, notes) -> dict ─────────────────────────
def h_me(c, uid, d, now, notes):
    return state(c, uid)


def h_shop(c, uid, d, now, notes):
    return {'amounts': SHOP_AMOUNTS}


def h_ref_link(c, uid, d, now, notes):
    return {'link': f'https://t.me/{bot_username()}?start=ref_{uid}'}


def h_heartbeat(c, uid, d, now, notes):
    c.execute('UPDATE users SET last_seen=? WHERE id=?', (now, uid))
    row = c.execute('SELECT * FROM referrals WHERE referred_id=?', (uid,)).fetchone()
    if not (row and not row['paid'] and d.get('visible') and now - row['started_at'] >= REF_SECONDS):
        return {'ok': True}
    ref = get_user(c, row['referrer_id'])
    if ref['referrals'] >= REF_MAX:
        return {'ok': True}
    c.execute('UPDATE users SET referrals=referrals+1 WHERE id=?', (ref['id'],))
    earn(c, ref['id'], REF_BONUS, 'referral')
    c.execute('UPDATE referrals SET paid=1,qualified_at=? WHERE referred_id=?', (now, uid))
    me = get_user(c, uid)
    name = '@' + me['username'] if me['username'] else str(uid)
    when = time.strftime('%d.%m.%Y, %H:%M', time.localtime(row['started_at']))
    notes.append((ref['id'], f'🎉 Новый реферал!\n👤 Пользователь: {name}\n🕐 Перешёл: {when}\n'
                  f'⏱ Пробыл в Mini App: 1 минута\n\n✅ Вы получили +{REF_BONUS} ⭐ Farm Stars за реферала!\n'
                  f'Всего приглашено: {ref["referrals"] + 1}/{REF_MAX}'))
    notes.append((uid, f'🎉 Вы перешли по реферальной ссылке пользователя @{ref["username"] or ref["id"]}.'))
    return {'ok': True, 'qualified': True}


def h_plant(c, uid, d, now, notes):
    r = get_user(c, uid)
    cur, _ = energy_now(r, now)
    if r['crop_stage'] != 'empty':
        raise ApiError('Поле уже занято')
    if cur < 1:
        raise ApiError('Нет энергии')
    c.execute('UPDATE users SET energy=?,energy_ts=?,crop_stage="growing",crop_planted=?,crop_ready=? WHERE id=?',
              (cur - 1, now, now, now + max(60, 300 - r['farm_level'] * 2), uid))
    return state(c, uid)


def h_harvest(c, uid, d, now, notes):
    r = get_user(c, uid)
    cur, _ = energy_now(r, now)
    if r['crop_stage'] == 'empty':
        raise ApiError('Сначала посадите пшеницу')
    if now < r['crop_ready']:
        raise ApiError('Пшеница ещё не созрела')
    if cur < 1:
        raise ApiError('Нет энергии')
    amount = 10 + r['farm_level'] * 3 + rng.randint(0, max(2, r['farm_level']))
    c.execute('UPDATE users SET wheat=wheat+?,energy=?,energy_ts=?,crop_stage="empty",crop_planted=0,crop_ready=0 '
              'WHERE id=?', (amount, cur - 1, now, uid))
    return state(c, uid)


def h_upgrade(c, uid, d, now, notes):
    r = get_user(c, uid)
    cost = upgrade_cost(r['farm_level'])
    if r['farm_level'] >= 100:
        raise ApiError('Максимальный уровень')
    if r['wheat'] < cost:
        raise ApiError(f'Нужно ещё {cost - r["wheat"]} 🌾')
    c.execute('UPDATE users SET wheat=wheat-?,farm_level=farm_level+1 WHERE id=?', (cost, uid))
    return state(c, uid)


def h_invoice(c, uid, d, now, notes):
    amount = int(d.get('amount', 0))
    if amount not in SHOP_AMOUNTS:
        raise ApiError('Некорректная сумма')
    payload = f'topup:{uid}:{amount}:{secrets.token_hex(8)}'
    c.execute('INSERT INTO payments(user_id,amount,payload,created_at) VALUES(?,?,?,?)', (uid, amount, payload, now))
    notes.append(('invoice', uid, amount, payload))     # created after commit, see Handler
    return {}


def h_daily(c, uid, d, now, notes):
    if now < get_user(c, uid)['daily_next']:
        raise ApiError('Daily Roulette ещё недоступна')
    reward = pick([(a, w / 100) for a, w in DAILY])
    c.execute('UPDATE users SET daily_next=? WHERE id=?', (now + 86400, uid))
    earn(c, uid, reward, 'daily')
    return {'reward': reward, 'state': state(c, uid)}


def spin(c, uid, d, table, typ):
    stake = stake_of(d)
    spend(c, uid, stake, typ + ':bet')
    mult = pick(table)
    reward = int(math.floor(stake * mult))
    earn(c, uid, reward, typ + ':win')
    record(c, uid, typ, reward)
    return {'multiplier': mult, 'reward': reward, 'history': history(c, uid, typ), 'state': state(c, uid)}


def h_roulette(c, uid, d, now, notes):
    return spin(c, uid, d, ROULETTE, 'roulette')


def h_lucky(c, uid, d, now, notes):
    return spin(c, uid, d, LUCKY, 'lucky')


def h_higher(c, uid, d, now, notes):
    stake, choice = stake_of(d), d.get('choice')
    if choice not in ('higher', 'lower'):
        raise ApiError('Некорректные данные')
    r = c.execute('SELECT value FROM results WHERE user_id=? AND type="higher_current" ORDER BY id DESC LIMIT 1',
                  (uid,)).fetchone()
    old = int(r['value']) if r else 50
    p = (100 - old) / 100 if choice == 'higher' else (old - 1) / 100      # chance to win (ties lose)
    if p < .05 or p > .9:
        raise ApiError('Для этого числа выбор слишком невыгоден/очевиден — выберите другую сторону')
    spend(c, uid, stake, 'higher:bet')
    n = rng.randint(1, 100)
    win = n > old if choice == 'higher' else n < old
    payout = int(math.floor(stake * EDGE / p)) if win else 0
    earn(c, uid, payout, 'higher:win')
    record(c, uid, 'higher_current', n)
    record(c, uid, 'higher', n)
    return {'number': n, 'win': win, 'payout': payout, 'history': history(c, uid, 'higher'), 'state': state(c, uid)}


# ── mines ──
def mine_mult(mines, safe):
    m = EDGE * math.comb(25, safe) / math.comb(25 - mines, safe)
    return round(min(MAX_MULT, max(1.0, m)), 2)


def active_game(c, uid, typ):
    return c.execute('SELECT * FROM games WHERE id=? AND status="active"', (f'{uid}:{typ}',)).fetchone()


def h_mines_start(c, uid, d, now, notes):
    stake, mines = stake_of(d), int(d.get('mines', 2))
    if mines not in (2, 5, 7):
        raise ApiError('Некорректное число мин')
    if active_game(c, uid, 'mines'):
        raise ApiError('Сначала завершите текущую игру (заберите выигрыш)')
    spend(c, uid, stake, 'mines:bet')
    dat = {'mines': rng.sample(range(25), mines), 'open': [], 'mine_count': mines, 'multiplier': 1.0}
    c.execute('INSERT OR REPLACE INTO games VALUES(?,?,?,?,?,?,?,?)',
              (f'{uid}:mines', uid, 'mines', 'active', stake, json.dumps(dat), now, now))
    st = state(c, uid)
    st['mine'] = {'open': []}
    return {'state': st}


def h_mines_open(c, uid, d, now, notes):
    g = active_game(c, uid, 'mines')
    if not g:
        raise ApiError('Нет активной игры')
    idx, dat = int(d.get('index', -1)), json.loads(g['data'])
    if idx < 0 or idx >= 25 or idx in dat['open']:
        raise ApiError('Некорректная клетка')
    if idx in dat['mines']:
        c.execute('UPDATE games SET status="lost",updated_at=? WHERE id=?', (now, g['id']))
        st = state(c, uid)
        st['mine'] = {'open': dat['open']}
        return {'mine': True, 'lost': g['stake'], 'multiplier': 1, 'mines': dat['mines'], 'state': st}
    dat['open'].append(idx)
    dat['multiplier'] = mine_mult(dat['mine_count'], len(dat['open']))
    c.execute('UPDATE games SET data=?,updated_at=? WHERE id=?', (json.dumps(dat), now, g['id']))
    st = state(c, uid)
    st['mine'] = {'open': dat['open']}
    return {'mine': False, 'multiplier': dat['multiplier'], 'state': st}


def h_mines_cashout(c, uid, d, now, notes):
    g = active_game(c, uid, 'mines')
    if not g:
        raise ApiError('Нет активной игры')
    dat = json.loads(g['data'])
    payout = int(math.floor(g['stake'] * float(dat.get('multiplier', 1))))
    earn(c, uid, payout, 'mines:cashout')
    c.execute('UPDATE games SET status="cashed",updated_at=? WHERE id=?', (now, g['id']))
    st = state(c, uid)
    st['mine'] = {'open': dat['open']}
    return {'payout': payout, 'state': st}


# ── rocket ──
def rocket_crash():
    x = EDGE / (1 - rng.random())          # P(crash >= m) = 0.95/m  ->  RTP 95% at any cash-out point
    return min(MAX_MULT, max(1.0, round(x, 2)))


def rocket_mult(dat, now):
    return min(dat['crash'], 1 + (now - dat['started']) * 0.55)


def settle_rocket_loss(c, g, dat, uid, now):
    c.execute('UPDATE games SET status="lost",updated_at=? WHERE id=?', (now, g['id']))
    record(c, uid, 'rocket', f'{dat["crash"]:.2f}')


def h_rocket_start(c, uid, d, now, notes):
    stake = stake_of(d)
    g = active_game(c, uid, 'rocket')
    if g:
        dat = json.loads(g['data'])
        if rocket_mult(dat, now) < dat['crash'] - 1e-9:
            raise ApiError('Раунд уже идёт')
        settle_rocket_loss(c, g, dat, uid, now)
    spend(c, uid, stake, 'rocket:bet')
    dat = {'stake': stake, 'crash': rocket_crash(), 'started': now}
    c.execute('INSERT OR REPLACE INTO games VALUES(?,?,?,?,?,?,?,?)',
              (f'{uid}:rocket', uid, 'rocket', 'active', stake, json.dumps(dat), now, now))
    return {'state': state(c, uid), 'history': history(c, uid, 'rocket', float)}


def h_rocket_state(c, uid, d, now, notes):
    g = c.execute('SELECT * FROM games WHERE id=? AND type="rocket"', (f'{uid}:rocket',)).fetchone()
    if not g:
        return {'active': False, 'crash': 1, 'state': state(c, uid)}
    dat = json.loads(g['data'])
    mult = rocket_mult(dat, now)
    active = g['status'] == 'active' and mult < dat['crash'] - 1e-9
    if g['status'] == 'active' and not active:
        settle_rocket_loss(c, g, dat, uid, now)
    out = {'active': active, 'multiplier': mult, 'state': state(c, uid)}
    out['crash'] = 1 if active else dat['crash']      # the crash point is revealed only after the round ends
    if not active:
        out['history'] = history(c, uid, 'rocket', float)
    return out


def h_rocket_cashout(c, uid, d, now, notes):
    g = active_game(c, uid, 'rocket')
    if not g:
        raise ApiError('Нет активного раунда')
    dat = json.loads(g['data'])
    mult = rocket_mult(dat, now)
    if mult >= dat['crash'] - 1e-9:
        settle_rocket_loss(c, g, dat, uid, now)
        raise ApiError('🚀 Ракета уже сгорела')       # NB: raising rolls back; loss is settled by /rocket/state
    payout = int(math.floor(g['stake'] * mult))
    earn(c, uid, payout, 'rocket:cashout')
    c.execute('UPDATE games SET status="cashed",updated_at=? WHERE id=?', (now, g['id']))
    record(c, uid, 'rocket', f'{mult:.2f}')
    return {'payout': payout, 'multiplier': mult, 'history': history(c, uid, 'rocket', float), 'state': state(c, uid)}


GET_ROUTES = {'/api/me': h_me, '/api/shop': h_shop, '/api/referral/link': h_ref_link}
POST_ROUTES = {
    '/api/referral/heartbeat': h_heartbeat, '/api/farm/plant': h_plant, '/api/farm/harvest': h_harvest,
    '/api/farm/upgrade': h_upgrade, '/api/payments/create-invoice': h_invoice, '/api/game/daily': h_daily,
    '/api/game/roulette': h_roulette, '/api/game/lucky': h_lucky, '/api/game/higher': h_higher,
    '/api/game/mines/start': h_mines_start, '/api/game/mines/open': h_mines_open,
    '/api/game/mines/cashout': h_mines_cashout, '/api/game/rocket/start': h_rocket_start,
    '/api/game/rocket/state': h_rocket_state, '/api/game/rocket/cashout': h_rocket_cashout}


# ───────────────────────── rate limit ─────────────────────────
_buckets = {}
_bl = threading.Lock()


def rate_ok(uid):
    now = time.time()
    with _bl:
        tokens, ts = _buckets.get(uid, (RATE_BURST, now))
        tokens = min(RATE_BURST, tokens + (now - ts) * RATE_PER_SEC)
        if tokens < 1:
            _buckets[uid] = (tokens, now)
            return False
        _buckets[uid] = (tokens - 1, now)
        if len(_buckets) > 50000:
            _buckets.clear()
        return True


# ───────────────────────── HTTP ─────────────────────────
INDEX = Path(__file__).with_name('index.html')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def sendj(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            b = INDEX.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            return self.wfile.write(b)
        if path == '/health':
            return self.sendj({'ok': True, 'index_found': INDEX.exists()})
        self.api(path, GET_ROUTES, {})

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if n > 10_000:
                raise ApiError('Too large')
            data = json.loads(self.rfile.read(n) or '{}')
        except ApiError as e:
            return self.sendj({'error': str(e)}, 400)
        except Exception:
            return self.sendj({'error': 'Bad JSON'}, 400)
        self.api(path, POST_ROUTES, data)

    def api(self, path, routes, data):
        fn = routes.get(path)
        if not fn:
            return self.sendj({'error': 'Unknown endpoint'}, 404)
        notes = []
        try:
            user = parse_init(self.headers.get('X-Telegram-Init-Data', ''))
            if not rate_ok(int(user['id'])):
                return self.sendj({'error': 'Слишком часто, подождите секунду'}, 429)
            with transaction() as c:
                uid = ensure_user(c, user)
                out = fn(c, uid, data if isinstance(data, dict) else {}, time.time(), notes)
        except ApiError as e:
            return self.sendj({'error': str(e)}, 400)
        except Exception as e:
            print('api error', path, repr(e))
            return self.sendj({'error': 'Server error'}, 500)
        # side effects only after a successful commit
        for n in notes:
            if n[0] == 'invoice':
                _, uid, amount, payload = n
                try:
                    r = tg('createInvoiceLink', {
                        'title': 'ботру — Farm Stars', 'description': f'Пополнение Farm Stars на {amount} ⭐',
                        'payload': payload, 'currency': 'XTR',
                        'prices': json.dumps([{'label': f'{amount} Farm Stars', 'amount': amount}])})
                    out = {'url': r['result']}
                except Exception as e:
                    print('invoice error', e)
                    return self.sendj({'error': 'Не удалось создать счёт'}, 502)
            else:
                send(n[0], n[1])
        self.sendj(out)


# ───────────────────────── bot polling ─────────────────────────
def handle_payment(m):
    sp = m['successful_payment']
    with transaction() as c:
        pay = c.execute('SELECT * FROM payments WHERE payload=?', (sp['invoice_payload'],)).fetchone()
        ok = (pay and not pay['credited'] and sp.get('currency') == 'XTR'
              and int(sp['total_amount']) == pay['amount'] and int(m['from']['id']) == pay['user_id'])
        if ok:
            earn(c, pay['user_id'], pay['amount'], f'purchase:{pay["id"]}')
            c.execute('UPDATE payments SET credited=1,charge_id=? WHERE id=?',
                      (sp.get('telegram_payment_charge_id'), pay['id']))
    if ok:
        send(pay['user_id'], f'✅ Оплата подтверждена! +{pay["amount"]} ⭐ Farm Stars.', web_markup())


def is_admin(uid):
    return int(uid) in ADMIN_IDS


def admin_command(uid, text):
    parts = text.split()
    cmd = parts[0].split('@')[0]
    if cmd == '/stats':
        with transaction() as c:
            def q(sql, args=()):
                return c.execute(sql, args).fetchone()[0] or 0
            day = time.time() - 86400
            players = q('SELECT COUNT(*) FROM users')
            active = q('SELECT COUNT(*) FROM users WHERE last_seen>?', (day,))
            bought = q('SELECT SUM(amount) FROM payments WHERE credited=1 AND refunded=0')
            refunded = q('SELECT SUM(amount) FROM payments WHERE refunded=1')
            bets = -q("SELECT SUM(delta) FROM ledger WHERE reason LIKE '%:bet'")
            wins = q("SELECT SUM(delta) FROM ledger WHERE reason LIKE '%:win' OR reason LIKE '%:cashout'")
            total = q('SELECT SUM(stars) FROM users')
        return (f'👥 Игроков: {players} (за 24ч: {active})\n'
                f'💰 Куплено ⭐: {bought}\n'
                f'↩️ Возвраты ⭐: {refunded}\n'
                f'🎲 Поставлено: {bets} ⭐, выплачено: {wins} ⭐\n'
                f'⭐ Всего на балансах: {total}')
    if cmd == '/payments' and len(parts) == 2 and parts[1].isdigit():
        with transaction() as c:
            rows = c.execute('SELECT id,amount,credited,refunded,created_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 10',
                             (int(parts[1]),)).fetchall()
        return '\n'.join(f'#{r["id"]}: {r["amount"]}⭐ credited={r["credited"]} refunded={r["refunded"]} '
                         f'{time.strftime("%d.%m %H:%M", time.localtime(r["created_at"]))}' for r in rows) or 'Нет платежей'
    if cmd == '/ledger' and len(parts) == 2 and parts[1].isdigit():
        with transaction() as c:
            rows = c.execute('SELECT delta,reason,balance,created_at FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 25',
                             (int(parts[1]),)).fetchall()
        return '\n'.join(f'{time.strftime("%d.%m %H:%M:%S", time.localtime(r["created_at"]))} {r["delta"]:+d} '
                         f'{r["reason"]} → {r["balance"]}' for r in rows) or 'Пусто'
    if cmd == '/refund' and len(parts) == 2 and parts[1].isdigit():
        with transaction() as c:
            pay = c.execute('SELECT * FROM payments WHERE id=?', (int(parts[1]),)).fetchone()
            if not pay or not pay['credited'] or pay['refunded'] or not pay['charge_id']:
                return 'Платёж не найден, не оплачен или уже возвращён'
        try:
            r = tg('refundStarPayment', {'user_id': pay['user_id'], 'telegram_payment_charge_id': pay['charge_id']})
        except Exception as e:
            return f'Telegram отклонил возврат: {e}'
        if not r.get('ok'):
            return f'Telegram отклонил возврат: {r}'
        with transaction() as c:
            c.execute('UPDATE payments SET refunded=1 WHERE id=?', (pay['id'],))
            have = c.execute('SELECT stars FROM users WHERE id=?', (pay['user_id'],)).fetchone()['stars']
            take = min(have, pay['amount'])
            c.execute('UPDATE users SET stars=stars-? WHERE id=?', (take, pay['user_id']))
            log_ledger(c, pay['user_id'], -take, f'refund:{pay["id"]}')
        send(pay['user_id'], f'↩️ Платёж на {pay["amount"]} ⭐ возвращён.')
        return f'✅ Возврат #{pay["id"]} выполнен, списано {take} Farm Stars'
    return ('Команды: /stats, /payments <user_id>, /ledger <user_id>, /refund <payment_id>')


def backup_loop():
    while True:
        time.sleep(BACKUP_EVERY)
        try:
            make_backup()
        except Exception as e:
            print('backup error', repr(e))


def make_backup():
    dst_dir = Path(DB_PATH).parent / 'backups'
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / f'farmstar-{time.strftime("%Y%m%d-%H%M%S")}.db'
    src = connect()
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()
    for old in sorted(dst_dir.glob('farmstar-*.db'))[:-BACKUP_KEEP]:
        old.unlink()
    return dst


def process_updates():
    offset = 0
    while True:
        try:
            r = tg('getUpdates', {'offset': offset, 'timeout': 25,
                                  'allowed_updates': json.dumps(['message', 'callback_query', 'pre_checkout_query'])})
            for up in r.get('result', []):
                offset = up['update_id'] + 1
                if 'pre_checkout_query' in up:
                    q = up['pre_checkout_query']
                    with transaction() as c:
                        known = c.execute('SELECT 1 FROM payments WHERE payload=? AND credited=0',
                                          (q.get('invoice_payload'),)).fetchone()
                    tg('answerPreCheckoutQuery', {'pre_checkout_query_id': q['id'], 'ok': 'true' if known else 'false',
                                                  'error_message': None if known else 'Счёт недействителен'})
                elif 'message' in up:
                    m = up['message']
                    if m.get('successful_payment'):
                        handle_payment(m)
                    elif m.get('text', '').split('@')[0] in ('/paysupport', '/support', '/terms'):
                        cmd = m['text'].split('@')[0]
                        if cmd == '/terms':
                            msg = ('ℹ️ Farm Stars — виртуальная игровая валюта. Она не является деньгами, не обменивается '
                                   'на деньги и не выводится. Покупка Farm Stars за Telegram Stars — цифровая услуга.')
                        else:
                            msg = ('💬 Вопросы по оплате и игре: ' + (SUPPORT or 'напишите администратору бота') +
                                   '\nУкажите ваш ID: ' + str(m['from']['id']))
                        send(m['from']['id'], msg)
                    elif m.get('text', '').startswith('/') and is_admin(m['from']['id']) and \
                            m['text'].split()[0].split('@')[0] in ('/stats', '/payments', '/ledger', '/refund', '/admin'):
                        send(m['from']['id'], admin_command(m['from']['id'], m['text']))
                    elif m.get('text', '').startswith('/start'):
                        parts = m['text'].split(maxsplit=1)
                        arg = parts[1] if len(parts) > 1 else ''
                        ref = arg[4:] if arg.startswith('ref_') else None
                        with transaction() as c:
                            uid = ensure_user(c, m['from'], ref)
                            referred = bool(c.execute('SELECT 1 FROM referrals WHERE referred_id=?', (uid,)).fetchone())
                        if referred:
                            send(uid, '🎉 Вы перешли по реферальной ссылке. Откройте Mini App и оставайтесь в нём '
                                      f'минимум {REF_SECONDS} секунд, чтобы реферал был засчитан.', web_markup())
                        else:
                            send(uid, '🌟 Добро пожаловать в ботру!', web_markup())
                elif 'callback_query' in up:
                    q = up['callback_query']
                    uid, data = q['from']['id'], q.get('data')
                    with transaction() as c:
                        ensure_user(c, q['from'])
                    tg('answerCallbackQuery', {'callback_query_id': q['id']})
                    if data == 'shop':
                        send(uid, '🛒 Откройте магазин в Mini App: Telegram Stars → Farm Stars 1:1', web_markup())
                    elif data == 'ref':
                        send(uid, f'👥 Откройте Профиль → Пригласить друга. Бонус +{REF_BONUS} ⭐ после '
                                  f'{REF_SECONDS} секунд в Mini App, максимум {REF_MAX}.', web_markup())
        except Exception as e:
            print('poll error', repr(e))
            time.sleep(3)


def main():
    if BOT_TOKEN:
        threading.Thread(target=process_updates, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    print(f'Listening on {PORT}, DB={DB_PATH}, WEB_APP_URL={WEB_APP_URL}')
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
