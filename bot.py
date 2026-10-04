import os, json, time, hmac, hashlib, secrets, random, math, sqlite3, threading, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOT_TOKEN=os.getenv('BOT_TOKEN','').strip(); WEB_APP_URL=os.getenv('WEB_APP_URL','https://farm-star-telegram.onrender.com/').strip(); PORT=int(os.getenv('PORT','10000')); DB_PATH=os.getenv('DB_PATH','/data/farmstar.db');
Path(DB_PATH).parent.mkdir(parents=True,exist_ok=True)
if not BOT_TOKEN: print('WARNING: BOT_TOKEN is not set')

def db():
 c=sqlite3.connect(DB_PATH,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c

def init_db():
 c=db(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, stars INTEGER NOT NULL DEFAULT 50, wheat INTEGER NOT NULL DEFAULT 0, farm_level INTEGER NOT NULL DEFAULT 1, energy REAL NOT NULL DEFAULT 10, energy_ts REAL NOT NULL DEFAULT 0, crop_stage TEXT NOT NULL DEFAULT 'empty', crop_planted REAL NOT NULL DEFAULT 0, crop_ready REAL NOT NULL DEFAULT 0, daily_next REAL NOT NULL DEFAULT 0, referrer_id INTEGER, referrals INTEGER NOT NULL DEFAULT 0, referral_code TEXT UNIQUE, referral_pending INTEGER NOT NULL DEFAULT 0, referral_qualified INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_seen REAL NOT NULL DEFAULT 0, username_display TEXT, photo_url TEXT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,amount INTEGER NOT NULL,payload TEXT UNIQUE NOT NULL,charge_id TEXT UNIQUE,credited INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,type TEXT NOT NULL,status TEXT NOT NULL,stake INTEGER NOT NULL DEFAULT 0,data TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,type TEXT NOT NULL,value TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS referrals(id INTEGER PRIMARY KEY AUTOINCREMENT,referrer_id INTEGER NOT NULL,referred_id INTEGER UNIQUE NOT NULL,started_at REAL NOT NULL,qualified_at REAL,paid INTEGER NOT NULL DEFAULT 0);'''); c.commit(); c.close()
init_db()

# Safe migrations for databases created by earlier versions.
def migrate_db():
 c=db()
 cols={r['name'] for r in c.execute("PRAGMA table_info(users)").fetchall()}
 if 'photo_url' not in cols: c.execute("ALTER TABLE users ADD COLUMN photo_url TEXT")
 c.commit(); c.close()
migrate_db()

def tg(method,payload):
 if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN missing')
 data=urllib.parse.urlencode({k:v for k,v in payload.items() if v is not None}).encode(); req=urllib.request.Request(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',data=data); return json.loads(urllib.request.urlopen(req,timeout=20).read())

def send(uid,text,markup=None):
 try: return tg('sendMessage',{'chat_id':uid,'text':text,'reply_markup':json.dumps(markup) if markup else None})
 except Exception as e: print('send error',e)

def web_markup(): return {'inline_keyboard':[[{'text':'🌟 Открыть ботру','web_app':{'url':WEB_APP_URL}}],[{'text':'🛒 Пополнить Farm Stars','callback_data':'shop'}],[{'text':'👥 Рефералы','callback_data':'ref'}]]}

def ensure_user(u,ref=None):
 uid=int(u['id']); now=time.time(); c=db(); row=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
 if not row:
  code='FS'+secrets.token_urlsafe(6).replace('-','').replace('_','').upper(); refid=None
  if ref and str(ref).isdigit() and int(ref)!=uid: refid=int(ref)
  c.execute('INSERT INTO users(id,username,first_name,referrer_id,referral_code,created_at,last_seen,photo_url) VALUES(?,?,?,?,?,?,?,?)',(uid,u.get('username'),u.get('first_name',''),refid,code,now,now,u.get('photo_url'))); c.commit()
  if refid:
   rr=c.execute('SELECT id FROM users WHERE id=?',(refid,)).fetchone()
   if rr and c.execute('SELECT 1 FROM referrals WHERE referred_id=?',(uid,)).fetchone() is None:
    c.execute('INSERT INTO referrals(referrer_id,referred_id,started_at) VALUES(?,?,?)',(refid,uid,now)); c.execute('UPDATE users SET referral_pending=1 WHERE id=?',(uid,)); c.commit()
 else:
  c.execute('UPDATE users SET username=?,first_name=?,last_seen=?,photo_url=? WHERE id=?',(u.get('username'),u.get('first_name',''),now,u.get('photo_url'),uid)); c.commit()
 c.close(); return uid

def parse_init(raw):
 if not raw or not BOT_TOKEN: raise ValueError('Telegram authorization required')
 vals=dict(urllib.parse.parse_qsl(raw,keep_blank_values=True)); got=vals.pop('hash',None)
 if not got: raise ValueError('Bad initData')
 check='\n'.join(f'{k}={vals[k]}' for k in sorted(vals)); secret=hmac.new(b'WebAppData',BOT_TOKEN.encode(),hashlib.sha256).digest(); want=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
 if not hmac.compare_digest(want,got): raise ValueError('Bad initData signature')
 auth=int(vals.get('auth_date','0')); 
 if time.time()-auth>86400: raise ValueError('initData expired')
 return json.loads(vals['user'])

def auth(headers): return parse_init(headers.get('X-Telegram-Init-Data',''))

def user_row(uid):
 c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close();
 if not r: raise ValueError('User not found')
 return r

def energy(r):
 now=time.time(); mx=min(100,10+r['farm_level']-1); cur=min(mx,float(r['energy'])+(now-float(r['energy_ts'] or now))/60); return cur,mx

def farm_state(r):
 cur,mx=energy(r); level=r['farm_level']; cost=max(20,int(40*level**1.25)); return {'level':level,'wheat':r['wheat'],'energy':int(cur),'energy_max':mx,'crop_stage':r['crop_stage'],'ready_at':r['crop_ready'],'upgrade_cost':cost}

def state(uid):
 c=db(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return {'user':{'id':r['id'],'username':r['username'],'name':r['first_name'] or r['username'] or 'Игрок','photo_url':r['photo_url']},'stars':r['stars'],'referrals':r['referrals'],'daily_next':r['daily_next'],'farm':farm_state(r)}

def change_energy(c,rid,amount):
 r=c.execute('SELECT * FROM users WHERE id=?',(rid,)).fetchone(); cur,mx=energy(r); cur-=amount
 if cur<0: raise ValueError('Недостаточно энергии')
 c.execute('UPDATE users SET energy=?,energy_ts=? WHERE id=?',(cur,time.time(),rid))

def result(uid,typ,value):
 c=db(); c.execute('INSERT INTO results(user_id,type,value,created_at) VALUES(?,?,?,?)',(uid,typ,str(value),time.time())); c.commit(); c.close()

def credit(uid,n):
 c=db(); c.execute('UPDATE users SET stars=stars+? WHERE id=?',(int(n),uid)); c.commit(); c.close()

def require_stars(c,uid,n):
 r=c.execute('SELECT stars FROM users WHERE id=?',(uid,)).fetchone();
 if r['stars']<n: raise ValueError(f'Недостаточно Farm Stars. Не хватает {n-r["stars"]} ⭐')
 c.execute('UPDATE users SET stars=stars-? WHERE id=?',(n,uid))

def payout(uid,n):
 c=db(); c.execute('UPDATE users SET stars=stars+? WHERE id=?',(int(n),uid)); c.commit(); c.close()

def game_id(uid,typ): return f'{uid}:{typ}'

def rocket_crash():
 # capped 100x, with much heavier probability near 1x
 u=random.random(); x=1.0/(1-u*0.99); return min(100.0,max(1.01,round(x,2)))

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def sendj(self,obj,status=200):
  b=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or '{}')
 def do_OPTIONS(self): self.send_response(204); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Access-Control-Allow-Headers','Content-Type,X-Telegram-Init-Data'); self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS'); self.end_headers()
 def do_GET(self):
  try:
   if self.path=='/' or self.path=='/index.html': return self.file()
   if self.path=='/health': return self.sendj({'ok':True,'index_found':Path('/app/index.html').exists() or Path(__file__).with_name('index.html').exists()})
   if self.path.startswith('/api/'):
    uid=ensure_user(auth(self.headers)); p=self.path.split('?')[0]
    if p=='/api/me': return self.sendj(state(uid))
    if p=='/api/shop': return self.sendj({'amounts':[10,25,50,100,250,500,1000]})
    if p=='/api/referral/link':
     r=user_row(uid); return self.sendj({'link':f'https://t.me/{BOT_USERNAME()}?start=ref_{r["id"]}'})
   self.send_response(404); self.end_headers()
  except Exception as e: self.sendj({'error':str(e)},400)
 def file(self):
  p=Path(__file__).with_name('index.html'); b=p.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.end_headers(); self.wfile.write(b)
 def do_POST(self):
  try:
   p=self.path.split('?')[0]; data=self.body(); uid=ensure_user(auth(self.headers)); c=db(); now=time.time()
   if p=='/api/referral/heartbeat':
    visible=bool(data.get('visible')); c.execute('UPDATE users SET last_seen=? WHERE id=?',(now,uid)); row=c.execute('SELECT * FROM referrals WHERE referred_id=?',(uid,)).fetchone()
    if row and not row['paid'] and visible and now-row['started_at']>=60:
     ref=c.execute('SELECT referrals FROM users WHERE id=?',(row['referrer_id'],)).fetchone()
     if ref and ref['referrals']<5:
      c.execute('UPDATE users SET stars=stars+10,referrals=referrals+1 WHERE id=?',(row['referrer_id'],)); c.execute('UPDATE referrals SET paid=1,qualified_at=? WHERE referred_id=?',(now,uid)); c.commit(); c.close(); rr=user_row(row['referrer_id']); u=user_row(uid); name='@'+u['username'] if u['username'] else str(uid); send(row['referrer_id'],f'🎉 Новый реферал!\n👤 Пользователь: {name}\n🕐 Перешёл: {time.strftime("%d.%m.%Y, %H:%M",time.localtime(row["started_at"]))}\n⏱ Пробыл в Mini App: 1 минута\n\n✅ Вы получили +10 ⭐ Farm Stars за реферала!\nВсего приглашено: {rr["referrals"]+1}/5'); send(uid,f'🎉 Вы перешли по реферальной ссылке пользователя @{rr["username"] or rr["id"]}.'); return self.sendj({'ok':True,'qualified':True})
    c.commit(); c.close(); return self.sendj({'ok':True})
   if p=='/api/farm/plant':
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); cur,mx=energy(r)
    if r['crop_stage']!='empty': raise ValueError('Поле уже занято')
    if cur<1: raise ValueError('Нет энергии')
    cur-=1; ready=now+max(60,300-r['farm_level']*2); c.execute('UPDATE users SET energy=?,energy_ts=?,crop_stage="growing",crop_planted=?,crop_ready=? WHERE id=?',(cur,now,now,ready,uid)); c.commit(); c.close(); return self.sendj(state(uid))
   if p=='/api/farm/harvest':
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); cur,mx=energy(r)
    if r['crop_stage']!='ready' and now<r['crop_ready']: raise ValueError('Пшеница ещё не созрела')
    if cur<1: raise ValueError('Нет энергии')
    amount=10+r['farm_level']*3+random.randint(0,max(2,r['farm_level']))
    cur-=1; c.execute('UPDATE users SET wheat=wheat+?,energy=?,energy_ts=?,crop_stage="empty",crop_planted=0,crop_ready=0 WHERE id=?',(amount,cur,now,uid)); c.commit(); c.close(); return self.sendj(state(uid))
   if p=='/api/farm/upgrade':
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); cost=max(20,int(40*r['farm_level']**1.25));
    if r['farm_level']>=100: raise ValueError('Максимальный уровень')
    if r['wheat']<cost: raise ValueError(f'Нужно ещё {cost-r["wheat"]} 🌾')
    c.execute('UPDATE users SET wheat=wheat-?,farm_level=farm_level+1 WHERE id=?',(cost,uid)); c.commit(); c.close(); return self.sendj(state(uid))
   if p=='/api/payments/create-invoice':
    amount=int(data.get('amount',0));
    if amount<1 or amount>100000: raise ValueError('Некорректная сумма')
    payload=f'topup:{uid}:{amount}:{secrets.token_hex(8)}'; c.execute('INSERT INTO payments(user_id,amount,payload,created_at) VALUES(?,?,?,?)',(uid,amount,payload,now)); c.commit(); c.close(); rr=tg('createInvoiceLink',{'title':'ботру — Farm Stars','description':f'Пополнение Farm Stars на {amount} ⭐','payload':payload,'currency':'XTR','prices':json.dumps([{'label':f'{amount} Farm Stars','amount':amount}])}); return self.sendj({'url':rr['result']})
   if p=='/api/game/daily':
    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();
    if now<r['daily_next']: raise ValueError('Daily Roulette ещё недоступна')
    roll=random.random()*100; acc=0
    for amt,w in [(5,40),(15,25),(25,15),(50,10),(75,7),(100,3)]:
     acc+=w
     if roll<acc: reward=amt; break
    c.execute('UPDATE users SET stars=stars+?,daily_next=? WHERE id=?',(reward,now+86400,uid)); c.commit(); c.close(); return self.sendj({'reward':reward,'state':state(uid)})
   if p=='/api/game/roulette':
    stake=int(data.get('stake',10));
    if stake<10 or stake%10: raise ValueError('Ставка минимум 10 и только шагом 10')
    require_stars(c,uid,stake); roll=random.random(); reward=0
    # five payout sectors + empty; larger payout is rarer
    if roll<.42: reward=5
    elif roll<.67: reward=15
    elif roll<.83: reward=25
    elif roll<.93: reward=50
    elif roll<.97: reward=100
    c.execute('UPDATE users SET stars=stars+? WHERE id=?',(reward,uid)); c.execute('INSERT INTO results(user_id,type,value,created_at) VALUES(?,?,?,?)',(uid,'roulette',str(reward),now)); c.commit(); hist=[int(x['value']) for x in c.execute('SELECT value FROM results WHERE user_id=? AND type="roulette" ORDER BY id DESC LIMIT 10',(uid,)).fetchall()]; c.close(); return self.sendj({'reward':reward,'history':hist,'state':state(uid)})
   if p=='/api/game/mines/start':
    stake=int(data.get('stake',10)); mines=int(data.get('mines',2));
    if stake<10 or stake%10 or mines not in (2,5,7): raise ValueError('Некорректная ставка/мины')
    require_stars(c,uid,stake); mid=game_id(uid,'mines'); c.execute('DELETE FROM games WHERE id=?',(mid,)); minepos=random.sample(range(25),mines); dat={'mines':minepos,'open':[],'stake':stake,'mine_count':mines,'lost':False}; c.execute('INSERT INTO games VALUES(?,?,?,?,?,?,?,?)',(mid,uid,'mines','active',stake,json.dumps(dat),now,now)); c.commit(); c.close(); st=state(uid); st['mine']={'open':[]}; return self.sendj({'state':st})
   if p=='/api/game/mines/open':
    idx=int(data.get('index')); g=c.execute('SELECT * FROM games WHERE id=? AND user_id=? AND type="mines" AND status="active"',(game_id(uid,'mines'),uid)).fetchone();
    if not g: raise ValueError('Нет активной игры')
    d=json.loads(g['data']);
    if idx in d['open'] or idx<0 or idx>=25: raise ValueError('Некорректная клетка')
    if idx in d['mines']:
     d['lost']=True; c.execute('UPDATE games SET status="lost",data=?,updated_at=? WHERE id=?',(json.dumps(d),now,g['id'])); c.commit(); c.close(); st=state(uid); st['mine']={'open':d['open']}; return self.sendj({'mine':True,'lost':g['stake'],'multiplier':1,'state':st})
    d['open'].append(idx); safe=len(d['open']); n=d['mine_count']; multiplier=(25/(25-n))**safe*0.95; multiplier=round(max(1,multiplier),2); d['multiplier']=multiplier; c.execute('UPDATE games SET data=?,updated_at=? WHERE id=?',(json.dumps(d),now,g['id'])); c.commit(); c.close(); st=state(uid); st['mine']={'open':d['open']}; return self.sendj({'mine':False,'multiplier':multiplier,'state':st})
   if p=='/api/game/mines/cashout':
    g=c.execute('SELECT * FROM games WHERE id=? AND user_id=? AND type="mines" AND status="active"',(game_id(uid,'mines'),uid)).fetchone();
    if not g: raise ValueError('Нет активной игры')
    d=json.loads(g['data']); mult=float(d.get('multiplier',1)); payoutv=int(math.floor(g['stake']*mult)); c.execute('UPDATE users SET stars=stars+? WHERE id=?',(payoutv,uid)); c.execute('UPDATE games SET status="cashed",updated_at=? WHERE id=?',(now,g['id'])); c.commit(); c.close(); st=state(uid); st['mine']={'open':d['open']}; return self.sendj({'payout':payoutv,'state':st})
   if p=='/api/game/higher':
    stake=int(data.get('stake',10)); choice=data.get('choice');
    if stake<10 or stake%10 or choice not in ('higher','lower'): raise ValueError('Некорректные данные')
    require_stars(c,uid,stake); r=c.execute('SELECT value FROM results WHERE user_id=? AND type="higher_current" ORDER BY id DESC LIMIT 1',(uid,)).fetchone(); old=int(r['value']) if r else 50; n=random.randint(1,100); win=(n>old if choice=='higher' else n<old); payoutv=stake*2 if win else 0
    if payoutv:c.execute('UPDATE users SET stars=stars+? WHERE id=?',(payoutv,uid))
    c.execute('INSERT INTO results(user_id,type,value,created_at) VALUES(?,?,?,?)',(uid,'higher_current',str(n),now)); c.execute('INSERT INTO results(user_id,type,value,created_at) VALUES(?,?,?,?)',(uid,'higher',str(n),now)); c.commit(); hist=[int(x['value']) for x in c.execute('SELECT value FROM results WHERE user_id=? AND type="higher" ORDER BY id DESC LIMIT 10',(uid,)).fetchall()]; c.close(); return self.sendj({'number':n,'win':win,'payout':payoutv,'history':hist,'state':state(uid)})
   if p=='/api/game/rocket/start':
    stake=int(data.get('stake',10));
    if stake<10 or stake%10: raise ValueError('Ставка минимум 10 и шаг 10')
    require_stars(c,uid,stake); gid=game_id(uid,'rocket'); c.execute('DELETE FROM games WHERE id=?',(gid,)); crash=rocket_crash(); d={'stake':stake,'crash':crash,'started':now,'cashed':False}; c.execute('INSERT INTO games VALUES(?,?,?,?,?,?,?,?)',(gid,uid,'rocket','active',stake,json.dumps(d),now,now)); c.commit(); c.close(); hist=[float(x['value']) for x in c.execute('SELECT value FROM results WHERE user_id=? AND type="rocket" ORDER BY id DESC LIMIT 10',(uid,)).fetchall()]
    return self.sendj({'state':state(uid),'history':hist})
   if p=='/api/game/rocket/state':
    g=c.execute('SELECT * FROM games WHERE id=? AND user_id=? AND type="rocket"',(game_id(uid,'rocket'),uid)).fetchone();
    if not g:return self.sendj({'active':False,'crash':1,'state':state(uid)})
    d=json.loads(g['data']); elapsed=now-d['started']; mult=min(d['crash'],1+elapsed*0.55); active=mult<d['crash']-1e-9
    if not active and g['status']=='active': c.execute('UPDATE games SET status="lost",updated_at=? WHERE id=?',(now,g['id'])); c.commit()
    return self.sendj({'active':active,'multiplier':mult,'crash':d['crash'],'state':state(uid)})
   if p=='/api/game/rocket/cashout':
    g=c.execute('SELECT * FROM games WHERE id=? AND user_id=? AND type="rocket" AND status="active"',(game_id(uid,'rocket'),uid)).fetchone();
    if not g: raise ValueError('Нет активного раунда')
    d=json.loads(g['data']); mult=min(d['crash'],1+(now-d['started'])*0.55)
    if mult>=d['crash']-1e-9: c.execute('UPDATE games SET status="lost",updated_at=? WHERE id=?',(now,g['id'])); c.commit(); c.close(); raise ValueError('🚀 Ракета уже сгорела')
    payoutv=int(math.floor(g['stake']*mult)); c.execute('UPDATE users SET stars=stars+? WHERE id=?',(payoutv,uid)); c.execute('UPDATE games SET status="cashed",updated_at=? WHERE id=?',(now,g['id'])); c.execute('INSERT INTO results(user_id,type,value,created_at) VALUES(?,?,?,?)',(uid,'rocket',f'{mult:.2f}',now)); c.commit(); hist=[float(x['value']) for x in c.execute('SELECT value FROM results WHERE user_id=? AND type="rocket" ORDER BY id DESC LIMIT 10',(uid,)).fetchall()]; c.close(); return self.sendj({'payout':payoutv,'multiplier':mult,'history':hist,'state':state(uid)})
   c.close(); self.sendj({'error':'Unknown endpoint'},404)
  except Exception as e:
   try:c.close()
   except:pass
   self.sendj({'error':str(e)},400)

def BOT_USERNAME():
 try:
  r=tg('getMe',{}); return r['result']['username']
 except:return 'Farme_star_bot'

def process_updates():
 offset=0
 while True:
  try:
   r=tg('getUpdates',{'offset':offset,'timeout':25,'allowed_updates':json.dumps(['message','callback_query','pre_checkout_query'])})
   for up in r.get('result',[]):
    offset=up['update_id']+1
    if 'pre_checkout_query' in up:
     q=up['pre_checkout_query']; tg('answerPreCheckoutQuery',{'pre_checkout_query_id':q['id'],'ok':'true'}); continue
    if 'message' in up:
     m=up['message']; u=m.get('from',{}); text=m.get('text','');
     ref=None
     if text.startswith('/start'):
      parts=text.split(maxsplit=1); arg=parts[1] if len(parts)>1 else ''
      if arg.startswith('ref_'): ref=arg[4:]
      uid=ensure_user(u,ref)
      if ref and str(ref).isdigit() and int(ref)!=uid: send(uid,'🎉 Вы перешли по реферальной ссылке. Откройте Mini App и оставайтесь в нём минимум 60 секунд, чтобы реферал был засчитан.',web_markup())
      else: send(uid,'🌟 Добро пожаловать в ботру!',web_markup())
     elif m.get('successful_payment'):
      sp=m['successful_payment']; payload=sp['invoice_payload']; c=db(); pay=c.execute('SELECT * FROM payments WHERE payload=?',(payload,)).fetchone()
      if pay and not pay['credited'] and int(sp['total_amount'])==pay['amount'] and sp['currency']=='XTR': c.execute('UPDATE users SET stars=stars+? WHERE id=?',(pay['amount'],pay['user_id'])); c.execute('UPDATE payments SET credited=1,charge_id=? WHERE id=?',(sp.get('telegram_payment_charge_id'),pay['id'])); c.commit(); send(pay['user_id'],f'✅ Оплата подтверждена! +{pay["amount"]} ⭐ Farm Stars.',web_markup())
      c.close()
    if 'callback_query' in up:
     q=up['callback_query']; uid=q['from']['id']; data=q.get('data'); ensure_user(q['from']); tg('answerCallbackQuery',{'callback_query_id':q['id']})
     if data=='shop': send(uid,'🛒 Откройте магазин в Mini App: Telegram Stars → Farm Stars 1:1',web_markup())
     elif data=='ref': send(uid,'👥 Откройте Профиль → Пригласить друга. Бонус +10 ⭐ после 60 секунд в Mini App, максимум 5.',web_markup())
  except Exception as e: print('poll error',e); time.sleep(3)

def main():
 threading.Thread(target=process_updates,daemon=True).start(); print(f'Listening on {PORT}, DB={DB_PATH}, WEB_APP_URL={WEB_APP_URL}'); ThreadingHTTPServer(('0.0.0.0',PORT),Handler).serve_forever()
if __name__=='__main__': main()
