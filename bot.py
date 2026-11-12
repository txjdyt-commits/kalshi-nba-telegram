#!/usr/bin/env python3
"""Kalshi NBA favorite alerts. Python 3.9+, no third-party dependencies."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
import signal
import threading
from telegram_transport import Telegram, AlertDeferred
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BASE = 'https://external-api.kalshi.com/trade-api/v2'
ESPN = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard'
ET = ZoneInfo('America/New_York')
UTC = timezone.utc
ALIASES = {'GS': 'GSW', 'NY': 'NYK', 'SA': 'SAS', 'NO': 'NOP', 'UTAH': 'UTA', 'WSH': 'WAS', 'PHO': 'PHX'}
MONTHS = ('JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC')
LOG = logging.getLogger('nba-alerts')


def utcnow():
    return datetime.now(UTC)


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


def code(value):
    return ALIASES.get(value.upper(), value.upper())


def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'NBAFavoriteAlerts/1.0', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=15) as response:
        # Reject an old cached response when the server exposes its cache age.
        if int(response.headers.get('Age', '0')) > 120:
            raise RuntimeError('Data response is cached for more than 120 seconds')
        return json.load(response)


def api(path, **params):
    return get_json(BASE + path + ('?' + urllib.parse.urlencode(params) if params else ''))


def markets():
    result, cursor, seen = [], '', set()
    for _ in range(30):
        data = api('/markets', series_ticker='KXNBAGAME', status='open', limit=1000, cursor=cursor)
        result.extend(data['markets'])
        cursor = data.get('cursor', '')
        if not cursor:
            return result
        if cursor in seen:
            raise RuntimeError('Repeated market pagination cursor')
        seen.add(cursor)
    raise RuntimeError('Market pagination exceeded limit; refusing partial results')


def scoreboard(now):
    # Yesterday catches late West Coast games across Eastern midnight.
    result = {}
    today = now.astimezone(ET).date()
    for offset in (-1, 0):
        day = today + timedelta(days=offset)
        data = get_json(ESPN + '?' + urllib.parse.urlencode({'dates': day.strftime('%Y%m%d'), 'limit': 100}))
        for event in data['events']:
            competition = event['competitions'][0]
            teams = competition['competitors']
            if len(teams) != 2:
                continue
            status = competition.get('status', event.get('status', {}))
            typ = status.get('type', {})
            result[event['id']] = {
                'id': event['id'], 'start': event['date'],
                'name': event['shortName'], 'state': typ.get('state'),
                'status_name': typ.get('name'), 'completed': typ.get('completed', False),
                'detail': typ.get('shortDetail', ''),
                'teams': {code(t['team']['abbreviation']): t['team']['displayName'] for t in teams},
                'score': ' - '.join(f"{t['team']['abbreviation']} {t.get('score', '0')}" for t in teams),
                'fetched_at': utcnow().timestamp(),
            }
    return list(result.values())


def match_markets(game, items):
    """Match date AND exact pair of teams; ambiguous/rescheduled matches are skipped."""
    start = stamp(game['start']).astimezone(ET)
    prefix = f'KXNBAGAME-{start:%y}{MONTHS[start.month-1]}{start:%d}'
    groups = defaultdict(dict)
    for market in items:
        event = market.get('event_ticker', '')
        if not event.startswith(prefix) or market.get('market_type') != 'binary':
            continue
        team = code(market['ticker'].rsplit('-', 1)[-1])
        if team in game['teams']:
            if team in groups[event]:
                return None
            groups[event][team] = market
    matches = [group for group in groups.values() if set(group) == set(game['teams'])]
    return matches[0] if len(matches) == 1 else None


def quote(bid, ask, max_spread):
    try:
        bid, ask = Decimal(str(bid)), Decimal(str(ask))
        if not bid.is_finite() or not ask.is_finite():
            return None
        if not (Decimal(0) < bid <= ask < Decimal(1)) or ask - bid > max_spread:
            return None
        return (bid + ask) / 2, bid, ask
    except (InvalidOperation, TypeError, ValueError):
        return None


def market_quote(market, spread):
    if market.get('status') not in ('active', 'open'):
        return None
    if market.get('result'):
        return None
    return quote(market.get('yes_bid_dollars'), market.get('yes_ask_dollars'), spread)


def favorite(quotes):
    if len(quotes) != 2 or any(q is None for q in quotes.values()):
        return None
    ranked = sorted(quotes, key=lambda team: quotes[team][0], reverse=True)
    a, b = ranked
    # Require a unique favorite and broadly complementary two-team prices.
    if quotes[a][0] <= Decimal('.5') or quotes[a][0] == quotes[b][0]:
        return None
    if abs(quotes[a][0] + quotes[b][0] - 1) > Decimal('.05'):
        return None
    return a


def historical_favorite(game, pair, spread, fetch=api):
    """Use same-minute quotes, strictly before the scheduled tipoff."""
    cutoff = int(stamp(game['start']).timestamp()) - 1
    histories = {}
    for team, market in pair.items():
        ticker = urllib.parse.quote(market['ticker'], safe='')
        data = fetch(f'/series/KXNBAGAME/markets/{ticker}/candlesticks',
                     start_ts=cutoff - 1800, end_ts=cutoff, period_interval=1)
        histories[team] = {
            int(c['end_period_ts']): quote(c.get('yes_bid', {}).get('close_dollars'),
                                        c.get('yes_ask', {}).get('close_dollars'), spread)
            for c in data.get('candlesticks', [])
            if cutoff - 1800 <= int(c['end_period_ts']) <= cutoff
        }
    shared = set.intersection(*(set(h) for h in histories.values()))
    if not shared:
        return None
    # Do not search backwards past a tie or invalid latest quote for an old favorite.
    ts = max(shared)
    quotes = {team: history[ts] for team, history in histories.items()}
    team = favorite(quotes)
    if not team:
        return None
    return {'team': team, 'probability': str(quotes[team][0]), 'observed_at': ts,
            'source': 'historical pregame minute', 'start': game['start'],
            'ticker': pair[team]['ticker']}


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS favorites (game_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alerts (game_id TEXT PRIMARY KEY, state TEXT NOT NULL, message TEXT NOT NULL, updated TEXT NOT NULL);
        ''')

    def get(self, game_id):
        row = self.db.execute('SELECT data FROM favorites WHERE game_id=?', (game_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, game_id, record):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO favorites VALUES (?,?)', (game_id, json.dumps(record)))

    def clear(self, game_id):
        with self.db:
            self.db.execute('DELETE FROM favorites WHERE game_id=?', (game_id,))

    def alert_state(self, game_id):
        row = self.db.execute('SELECT state FROM alerts WHERE game_id=?', (game_id,)).fetchone()
        return row[0] if row else None

    def claim(self, game_id, message):
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO alerts VALUES (?,?,?,?)',
                                  (game_id, 'attempting', message, utcnow().isoformat()))
        return cur.rowcount == 1

    def mark(self, game_id, state):
        with self.db:
            self.db.execute('UPDATE alerts SET state=?, updated=? WHERE game_id=?',
                            (state, utcnow().isoformat(), game_id))


def live(game, now):
    return (game['state'] == 'in' and not game.get('completed')
            and game.get('status_name') in ('STATUS_IN_PROGRESS', 'STATUS_HALFTIME', 'STATUS_END_PERIOD')
            and stamp(game['start']) <= now < stamp(game['start']) + timedelta(hours=8)
            and 0 <= now.timestamp() - game['fetched_at'] <= 90)


def process(game, pair, db, now, config, send, history=historical_favorite, retry_history=None):
    gid = game['id']
    spread = Decimal(str(config.get('max_spread', '.10')))
    record = db.get(gid)
    start = stamp(game['start'])
    if game['state'] == 'pre' and now < start:
        quotes = {team: market_quote(m, spread) for team, m in pair.items()}
        team = favorite(quotes)
        if team:
            db.put(gid, {'team': team, 'probability': str(quotes[team][0]),
                        'observed_at': now.timestamp(), 'source': 'observed pregame quote',
                        'start': game['start'], 'ticker': pair[team]['ticker']})
        else:
            db.clear(gid)
        return
    if not live(game, now) or db.alert_state(gid):
        return
    # Refresh a baseline after schedule changes or a long pregame outage.
    valid = (record and record['start'] == game['start'] and record['team'] in pair
             and record['ticker'] == pair[record['team']]['ticker']
             and 0 < start.timestamp() - record['observed_at'] <= 1800)
    if not valid:
        retry_history = retry_history if retry_history is not None else {}
        if now.timestamp() < retry_history.get(gid, 0):
            return
        retry_history[gid] = now.timestamp() + 300
        record = history(game, pair, spread)
        if not record:
            LOG.warning('%s: no reliable pregame favorite; skipped', game['name'])
            return
        db.put(gid, record)
    q = market_quote(pair[record['team']], spread)
    if not q or q[0] < Decimal(str(config.get('threshold', '.75'))):
        return
    # Never send using a scoreboard that aged out while fetching history.
    if not live(game, utcnow()):
        return
    p, bid, ask = q
    message = (f"NBA alert: {game['teams'][record['team']]} (pregame favorite "
               f"{Decimal(record['probability']):.1%}) is now {p:.1%} on Kalshi. "
               f"YES bid/ask {bid*100:g}/{ask*100:g} cents. "
               f"{game['score']} | {game['detail']}. "
               f"{record['ticker']}")
    if db.claim(gid, message):
        try:
            send(message)
        except AlertDeferred:
            with db.db:
                db.db.execute('DELETE FROM alerts WHERE game_id=? AND state=?', (gid, 'attempting'))
        except Exception:
            db.mark(gid, 'uncertain')
            LOG.exception('Message attempt failed/uncertain for %s; no automatic resend. See README.', gid)
        else:
            db.mark(gid, 'submitted')
            LOG.info('Alert submitted for %s', game['name'])


def load_config():
    return {'threshold': os.getenv('THRESHOLD','0.75'),
            'poll_seconds': os.getenv('POLL_SECONDS','30'),
            'max_spread': os.getenv('MAX_SPREAD','0.10')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-message', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Print alerts; separate database; never send')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--retry-alert', metavar='GAME_ID', help='Clear an uncertain attempt after manually checking Telegram')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config()
    if not Decimal('.5') < Decimal(str(config.get('threshold', '.75'))) < Decimal('1'):
        parser.error('threshold must be between 0.5 and 1')
    interval = float(config.get('poll_seconds', 30))
    if interval < 10 or interval > 3600:
        parser.error('poll_seconds must be between 10 and 3600')
    state_dir = Path(os.getenv('STATE_DIR', str(ROOT / 'state')))
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = open(state_dir / ('preview.lock' if args.dry_run else 'bot.lock'), 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('Another instance is running. Stop it first.')
    db = Store(state_dir / ('preview.sqlite3' if args.dry_run else 'alerts.sqlite3'))
    if args.status:
        for row in db.db.execute('SELECT game_id,state,message,updated FROM alerts ORDER BY updated DESC'):
            print(row)
        return
    if args.retry_alert:
        with db.db:
            db.db.execute("DELETE FROM alerts WHERE game_id=? AND state IN ('uncertain','attempting')", (args.retry_alert,))
        print('Uncertain attempt cleared, if present. Restart monitoring; only a currently qualifying game can resend.')
        return
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    telegram = None
    if not args.dry_run:
        telegram = Telegram(os.getenv('TELEGRAM_BOT_TOKEN',''),os.getenv('PAIRING_CODE',''),
                            state_dir / 'telegram.sqlite3',stop,config['threshold'])
        telegram.verify()
        if args.test_message:
            telegram.send_alert('Test: hosted NBA alerts can reach your Telegram chat.')
            print('Telegram accepted the test message.')
            return
        threading.Thread(target=telegram.poll,daemon=True).start()
    send = (lambda msg: print('PREVIEW:',msg,flush=True)) if args.dry_run else telegram.send_alert
    LOG.info('Monitoring every %ss; threshold %s; %s',interval,config['threshold'],'PREVIEW ONLY' if args.dry_run else 'Telegram')
    retries, failures = {}, 0
    while not stop.is_set():
        cycle_started = time.monotonic()
        try:
            items = markets()
            LOG.info('Loaded %d open NBA winner markets', len(items))
            games = scoreboard(utcnow())
            matched = 0
            for game in games:
                if game['state'] not in ('pre', 'in'):
                    continue
                pair = match_markets(game, items)
                if not pair:
                    if game['state'] == 'in':
                        LOG.warning('%s: no unambiguous open Kalshi match', game['name'])
                    continue
                matched += 1
                try:
                    # Fresh quotes for live games, after any earlier historical calls.
                    if live(game, utcnow()):
                        pair = {team: api('/markets/' + urllib.parse.quote(m['ticker'], safe=''))['market'] for team, m in pair.items()}
                    process(game, pair, db, utcnow(), config, send, retry_history=retries)
                except Exception:
                    LOG.exception('Skipped %s after data error', game['name'])
            LOG.info('Checked %d games; %d matched; %d live', len(games), matched, sum(live(g, utcnow()) for g in games))
            if telegram:
                telegram.set('last_scan', utcnow().isoformat())
                telegram.set('scan_error', 'none')
            failures = 0
        except Exception:
            failures += 1
            if telegram:
                telegram.set('scan_error','Data feed unavailable; retrying')
            LOG.exception('Data check failed; alerts paused until a successful check')
            if args.once:
                raise
        if args.once:
            break
        delay = min(300, interval * (2 ** min(failures, 4)))
        stop.wait(max(1, delay - (time.monotonic() - cycle_started)))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nMonitoring stopped.')
