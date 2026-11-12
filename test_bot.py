import copy
from datetime import timedelta
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import bot


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.db = bot.Store(self.path)
        self.now = bot.utcnow()
        self.start = self.now - timedelta(minutes=20)
        self.game = {'id':'123', 'start':self.start.isoformat(), 'name':'BOS @ NY',
                     'teams':{'BOS':'Boston Celtics','NYK':'New York Knicks'},
                     'state':'in', 'completed':False, 'status_name':'STATUS_IN_PROGRESS',
                     'detail':'Q2 8:00', 'score':'BOS 44 - NYK 36', 'fetched_at':self.now.timestamp()}
        day = self.start.astimezone(bot.ET)
        event = f'KXNBAGAME-{day:%y}{bot.MONTHS[day.month-1]}{day:%d}BOSNYK'
        self.pair = {t:{'ticker':event+'-'+t, 'event_ticker':event, 'market_type':'binary',
                        'status':'active','yes_bid_dollars':b,'yes_ask_dollars':a}
                     for t,b,a in [('BOS','.74','.76'),('NYK','.24','.26')]}
        self.record = {'team':'BOS','probability':'.60','observed_at':self.start.timestamp()-60,
                       'source':'test','start':self.game['start'],'ticker':self.pair['BOS']['ticker']}
        self.db.put('123',self.record)
        self.sent = []

    def tearDown(self):
        self.db.db.close()
        self.tmp.cleanup()

    def run_check(self, history=None, sender=None):
        kwargs = {'history':history} if history else {}
        bot.process(self.game,self.pair,self.db,self.now,{},sender or self.sent.append,**kwargs)

    def test_at_75_sends_once_even_after_restart(self):
        self.run_check()
        self.db.db.close()
        self.db = bot.Store(self.path)
        self.run_check()
        self.assertEqual(len(self.sent),1)
        self.assertIn('75.0%',self.sent[0])

    def test_above_75(self):
        self.pair['BOS'].update(yes_bid_dollars='.80',yes_ask_dollars='.82')
        self.run_check()
        self.assertEqual(len(self.sent),1)

    def test_below_threshold(self):
        self.pair['BOS'].update(yes_bid_dollars='.73',yes_ask_dollars='.75')
        self.run_check()
        self.assertFalse(self.sent)

    def test_live_underdog_never_replaces_original_favorite(self):
        self.pair['BOS'].update(yes_bid_dollars='.20',yes_ask_dollars='.22')
        self.pair['NYK'].update(yes_bid_dollars='.78',yes_ask_dollars='.80')
        self.run_check()
        self.assertFalse(self.sent)
        self.assertEqual(self.db.get('123')['team'],'BOS')

    def test_pregame_never_sends(self):
        self.game.update(state='pre',start=(self.now+timedelta(hours=1)).isoformat())
        self.run_check()
        self.assertFalse(self.sent)

    def test_final_cancelled_and_delayed_never_send(self):
        for state,name,done in [('post','STATUS_FINAL',True),('in','STATUS_DELAYED',False),('pre','STATUS_POSTPONED',False)]:
            self.game.update(state=state,status_name=name,completed=done)
            self.run_check()
        self.assertFalse(self.sent)

    def test_stale_scoreboard(self):
        self.game['fetched_at'] -= 150
        self.run_check()
        self.assertFalse(self.sent)

    def test_wide_missing_crossed_closed_quotes(self):
        for changes in [{'yes_bid_dollars':'.60','yes_ask_dollars':'.90'},
                        {'yes_bid_dollars':None}, {'yes_bid_dollars':'.90','yes_ask_dollars':'.80'},
                        {'status':'closed'}, {'result':'yes'}]:
            original = copy.deepcopy(self.pair)
            self.pair['BOS'].update(changes)
            self.run_check()
            self.pair = original
        self.assertFalse(self.sent)

    def test_quote_rejects_nan_and_boundary(self):
        for b,a in [('NaN','.8'),('0','.01'),('.99','1'),('Infinity','.9')]:
            self.assertIsNone(bot.quote(b,a,Decimal('.1')))

    def test_absent_pregame_record_bootstraps(self):
        self.db.clear('123')
        self.run_check(history=lambda *args:self.record)
        self.assertEqual(len(self.sent),1)

    def test_missing_history_skips(self):
        self.db.clear('123')
        self.run_check(history=lambda *args:None)
        self.assertFalse(self.sent)

    def test_send_failure_not_retried_automatically(self):
        def fail(msg):
            raise RuntimeError('ambiguous delivery')
        with self.assertLogs('nba-alerts',level='ERROR'):
            self.run_check(sender=fail)
        self.run_check()
        self.assertEqual(self.db.alert_state('123'),'uncertain')
        self.assertFalse(self.sent)

    def test_match_exact_date_and_teams(self):
        items = list(self.pair.values())
        self.assertEqual(bot.match_markets(self.game,items),self.pair)
        bad = copy.deepcopy(items)
        for m in bad: m['event_ticker'] = 'KXNBAGAME-20JAN01BOSNYK'
        self.assertIsNone(bot.match_markets(self.game,bad))
        duplicate = copy.deepcopy(items)
        for m in duplicate: m['event_ticker'] += '2'
        self.assertIsNone(bot.match_markets(self.game,items+duplicate))

    def test_espn_aliases(self):
        self.assertEqual([bot.code(t) for t in ['GS','NY','SA','NO','UTAH','WSH']],['GSW','NYK','SAS','NOP','UTA','WAS'])

    def test_tie_no_favorite(self):
        q = bot.quote('.49','.51',Decimal('.1'))
        self.assertIsNone(bot.favorite({'BOS':q,'NYK':q}))

    def test_pregame_tie_clears_previous_baseline(self):
        self.game.update(state='pre',start=(self.now+timedelta(hours=1)).isoformat())
        for m in self.pair.values():m.update(yes_bid_dollars='.49',yes_ask_dollars='.51')
        self.run_check()
        self.assertIsNone(self.db.get('123'))

    def test_history_uses_only_before_tipoff(self):
        end = int(self.start.timestamp())
        def fake(path,**params):
            self.assertLess(params['end_ts'],end)
            b,a = ('.59','.61') if '/'+self.pair['BOS']['ticker']+'/' in path else ('.39','.41')
            return {'candlesticks':[{'end_period_ts':end-60,'yes_bid':{'close_dollars':b},'yes_ask':{'close_dollars':a}},
                                    {'end_period_ts':end+60,'yes_bid':{'close_dollars':'.95'},'yes_ask':{'close_dollars':'.97'}}]}
        record = bot.historical_favorite(self.game,self.pair,Decimal('.1'),fetch=fake)
        self.assertEqual(record['team'],'BOS')
        self.assertEqual(record['observed_at'],end-60)

    def test_history_latest_tie_does_not_use_older_favorite(self):
        end = int(self.start.timestamp())
        def fake(path,**params):
            b,a = ('.59','.61') if '/'+self.pair['BOS']['ticker']+'/' in path else ('.39','.41')
            return {'candlesticks':[{'end_period_ts':end-120,'yes_bid':{'close_dollars':b},'yes_ask':{'close_dollars':a}},
                                    {'end_period_ts':end-60,'yes_bid':{'close_dollars':'.49'},'yes_ask':{'close_dollars':'.51'}}]}
        self.assertIsNone(bot.historical_favorite(self.game,self.pair,Decimal('.1'),fetch=fake))



if __name__ == '__main__':
    unittest.main()
