import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from hermes_google_alerts import notify
from hermes_google_runner import atomic_json


class HermesGoogleAlertsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        client = self.state / 'mail-client'
        client.touch()
        config = self.state / 'config.toml'
        config.write_text('[accounts.owner]\nemail = "owner@example.com"\n', encoding='utf-8')
        atomic_json(self.state / 'notification.json',
                    {'executable': str(client), 'config': str(config), 'account': 'owner'})
        self.execution = {'execution_id': 'abc1234567890', 'state': 'complete'}
        self.status = {'issues': ['Coverage incomplete'], 'findings': []}

    def test_acceptance_is_recorded_and_an_unchanged_report_stays_quiet(self):
        mail = Mock(return_value=subprocess.CompletedProcess([], 0))
        self.assertEqual(notify(self.state, self.status, self.execution, atomic_json, run=mail)['state'],
                         'smtp_accepted')
        self.assertEqual(notify(self.state, self.status, self.execution, atomic_json, run=mail)['state'],
                         'unchanged')
        self.assertEqual(mail.call_count, 2)
        args = mail.call_args.args[0]
        self.assertEqual(args[args.index('--config') + 1], 'config.toml')
        self.assertEqual(mail.call_args.kwargs['cwd'], self.state)
        message = mail.call_args.kwargs['input']
        self.assertIn(b'To: owner@example.com', message)
        self.assertIn(b'Coverage incomplete', message)

    def test_uncertain_send_is_never_repeated_and_inbox_match_recovers_receipt(self):
        mail = Mock(side_effect=[subprocess.CompletedProcess([], 0),
                                 subprocess.TimeoutExpired('client', 45),
                                 subprocess.CompletedProcess([], 0, stdout=b'{"envelopes":[]}'),
                                 subprocess.CompletedProcess([], 0, stdout=json.dumps({'envelopes': [
                                     {'subject': 'BassBeerMap Google review BBM-abc123456789'}
                                 ]}).encode())])
        for expected in ('unconfirmed', 'unconfirmed', 'unchanged'):
            self.assertEqual(notify(self.state, self.status, self.execution, atomic_json, run=mail)['state'],
                             expected)
        self.assertEqual(sum(call.args[0][1:3] == ['message', 'send'] for call in mail.call_args_list), 1)
        journal = json.loads((self.state / 'notification-state.json').read_text())
        self.assertEqual(journal['accepted']['state'], 'inbox_verified')
