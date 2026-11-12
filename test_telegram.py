import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import bot
from telegram_transport import Telegram, TelegramError, AlertDeferred
import test_bot

class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'control.sqlite3'
        self.tg=Telegram('123:FAKE_TEST_TOKEN','test-pairing-code-1234',self.path,threading.Event())
        self.tg.call=Mock(return_value={'message_id':99})

    def tearDown(self):
        self.tmp.cleanup()

    def message(self,text,user=42,kind='private'):
        return {'update_id':1,'message':{'chat':{'id':user,'type':kind},'from':{'id':user,'is_bot':False},'text':text}}

    def pair(self):
        self.tg.handle(self.message('/start test-pairing-code-1234'))

    def test_wrong_code_and_groups_cannot_pair(self):
        self.tg.handle(self.message('/start wrong'))
        self.tg.handle(self.message('/start test-pairing-code-1234',kind='group'))
        self.assertIsNone(self.tg.get('owner'))
        self.tg.call.assert_not_called()

    def test_owner_persists_and_cannot_be_replaced(self):
        self.pair()
        other=Telegram('123:FAKE_TEST_TOKEN','test-pairing-code-1234',self.path,threading.Event())
        self.assertEqual(other.get('owner'),42)
        self.tg.handle(self.message('/start test-pairing-code-1234',user=17))
        self.assertEqual(self.tg.get('owner'),42)

    def test_unauthorized_user_cannot_pause(self):
        self.pair()
        self.tg.handle(self.message('/pause',user=17))
        self.assertTrue(self.tg.enabled())

    def test_pause_resume_and_alert_payload(self):
        self.pair()
        self.tg.handle(self.message('/pause'))
        with self.assertRaises(AlertDeferred):self.tg.send_alert('test')
        self.tg.handle(self.message('/resume'))
        self.assertEqual(self.tg.send_alert('test'),99)
        self.tg.call.assert_called_with('sendMessage',{'chat_id':42,'text':'test'})

    def test_unpaired_no_message(self):
        with self.assertRaises(AlertDeferred):self.tg.send_alert('test')
        self.tg.call.assert_not_called()

    def test_offset_and_pause_persist(self):
        self.tg.set('offset',125)
        self.tg.set('paused',True)
        other=Telegram('123:FAKE_TEST_TOKEN','test-pairing-code-1234',self.path,threading.Event())
        self.assertEqual(other.get('offset'),125)
        self.assertTrue(other.get('paused'))

    def test_existing_webhook_not_removed(self):
        self.tg.call.side_effect=[{'username':'examplebot'},{'url':'https://example.com/hook'}]
        with self.assertRaises(TelegramError):self.tg.verify()
        self.assertEqual(self.tg.call.call_count,2)

    def test_errors_hide_token_url(self):
        real=Telegram('123:SECRET_TOKEN','test-pairing-code-1234',self.path,threading.Event())
        error=urllib.error.URLError('https://api.telegram.org/bot123:SECRET_TOKEN/getMe')
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(TelegramError) as cm:real.call('getMe')
        self.assertNotIn('SECRET_TOKEN',str(cm.exception))
        self.assertTrue(cm.exception.__suppress_context__)

    def test_429_safe_defer(self):
        real=Telegram('123:SECRET_TOKEN','test-pairing-code-1234',self.path,threading.Event())
        error=urllib.error.HTTPError('url',429,'limit',{},io.BytesIO(b'{"parameters":{"retry_after":5}}'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(AlertDeferred):real.call('sendMessage')
        self.assertGreater(real.cooldown_until,0)

    def test_no_confirmation_is_uncertain(self):
        self.pair()
        self.tg.call.return_value={}
        with self.assertRaises(TelegramError):self.tg.send_alert('alert')

class DeferredDeliveryTest(unittest.TestCase):
    def test_pausing_does_not_consume_game_alert(self):
        fixture=test_bot.AlertTests()
        fixture.setUp()
        try:
            def paused(_):raise AlertDeferred('paused')
            fixture.run_check(sender=paused)
            self.assertIsNone(fixture.db.alert_state('123'))
            fixture.run_check()
            self.assertEqual(len(fixture.sent),1)
        finally:
            fixture.tearDown()

if __name__=='__main__':unittest.main()
