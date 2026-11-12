"""Private Telegram transport, pairing, controls, and persistent update offsets."""
import json
from contextlib import contextmanager
import logging
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
import urllib.error

LOG = logging.getLogger('nba-alerts.telegram')

class AlertDeferred(Exception):
    """No delivery attempted: unpaired, paused, or known rate limit."""

class TelegramError(Exception):
    pass

class Telegram:
    def __init__(self, token, pairing_code, state_path, stop, threshold='0.75'):
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token or ''):
            raise ValueError('Set TELEGRAM_BOT_TOKEN in your hosting secrets')
        if len(pairing_code or '') < 16:
            raise ValueError('PAIRING_CODE must contain at least 16 characters')
        self.token, self.pairing_code = token, pairing_code
        self.path, self.stop, self.threshold = state_path, stop, threshold
        self.lock = threading.RLock()
        self.cooldown_until = 0
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM control WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO control VALUES (?,?)',(key,json.dumps(value)))

    def call(self, method, payload=None):
        # Never propagate URL-containing urllib exceptions: URLs contain the bot token.
        req = urllib.request.Request('https://api.telegram.org/bot'+self.token+'/'+method,
                                     data=json.dumps(payload or {}).encode(),
                                     headers={'Content-Type':'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req,timeout=25) as r:
                data = json.load(r)
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status == 429:
                try:
                    seconds = max(1,min(3600,int(json.load(exc).get('parameters',{}).get('retry_after',60))))
                except Exception:
                    seconds = 60
                self.cooldown_until = time.time()+seconds
                raise AlertDeferred('Telegram rate limit; retry after cooldown') from None
            raise TelegramError(f'Telegram HTTP {status}; check bot configuration and permissions') from None
        except Exception:
            raise TelegramError('Telegram connection/response failure; delivery may be uncertain') from None
        if not data.get('ok'):
            raise TelegramError('Telegram rejected request; check configuration')
        return data['result']

    def verify(self):
        info = self.call('getMe')
        if self.call('getWebhookInfo').get('url'):
            raise TelegramError('This bot has a webhook. Use a dedicated bot; existing webhook was not changed.')
        self.set('bot_username',info['username'])
        LOG.info('Telegram connected; awaiting private pairing if not already paired')

    def enabled(self):
        return bool(self.get('owner')) and not self.get('paused',False)

    def send_alert(self, text):
        with self.lock:
            if not self.enabled() or time.time() < self.cooldown_until:
                raise AlertDeferred('Alerts paused, unpaired, or cooling down')
            result = self.call('sendMessage',{'chat_id':self.get('owner'),'text':text})
            if not isinstance(result,dict) or 'message_id' not in result:
                raise TelegramError('Telegram returned no message confirmation')
            return result['message_id']

    def handle(self, update):
        msg = update.get('message',{})
        chat, sender = msg.get('chat',{}), msg.get('from',{})
        if chat.get('type') != 'private' or sender.get('is_bot') or sender.get('id') != chat.get('id'):
            return
        text = msg.get('text','').strip()
        cmd, _, argument = text.partition(' ')
        cmd = cmd.split('@',1)[0].lower()
        chat_id = chat['id']
        with self.lock:
            owner = self.get('owner')
            if not owner:
                if cmd != '/start' or not secrets.compare_digest(argument.strip().encode(),self.pairing_code.encode()):
                    return
                self.set('owner',chat_id)
                self.set('paused',False)
                reply = 'Paired. NBA favorite alerts are enabled. Use /status, /pause, /resume, /test or /help.'
            elif owner != chat_id:
                return
            elif cmd in ('/pause','/stop'):
                self.set('paused',True)
                reply = 'Alerts paused. Send /resume to enable them again.'
            elif cmd in ('/resume','/start'):
                self.set('paused',False)
                reply = 'Alerts enabled. One alert per qualifying game; no replay of old alerts.'
            elif cmd == '/status':
                reply = ('Alerts: '+('enabled' if self.enabled() else 'paused')+
                         f'\nThreshold: {float(self.threshold):.0%} quote midpoint'+
                         '\nLast successful scan: '+self.get('last_scan','not yet completed')+
                         '\nRecent scan issue: '+self.get('scan_error','none'))
            elif cmd == '/test':
                reply = 'Test: your hosted NBA bot can reach this Telegram chat.'
            else:
                reply = 'Commands: /status /pause /resume /test /help. Live NBA only; pregame favorite; at least 75%; one alert per game.'
            self.call('sendMessage',{'chat_id':chat_id,'text':reply})

    def poll(self):
        while not self.stop.is_set():
            try:
                if time.time() < self.cooldown_until:
                    self.stop.wait(min(30,self.cooldown_until-time.time()))
                    continue
                updates = self.call('getUpdates',{'offset':self.get('offset',0),'timeout':15,'allowed_updates':['message']})
                for update in updates:
                    if self.stop.is_set():
                        break
                    try:
                        self.handle(update)
                    except (TelegramError,AlertDeferred):
                        LOG.warning('Telegram command reply failed; resend the command if needed')
                    # Commands are idempotent; persistent offset avoids replay after restarts.
                    self.set('offset',update['update_id']+1)
            except Exception:
                LOG.warning('Telegram polling unavailable; retrying without exposing credentials')
                self.stop.wait(10)
