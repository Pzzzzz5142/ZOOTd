import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from maa_planner.copilot_local import LocalCopilotClient
from maa_planner.copilot_run import ExperimentError, experiment
from maa_planner.prts import PrtsError, StageCatalog


class LocalCopilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'input.json'
        self.snapshot = self.root / 'snapshot.json'
        self.catalog = StageCatalog([{'code': 'S-1', 'stageId': 'stage'}])
        self.content = {'stage_name': 'stage', 'difficulty': 3,
                        'doc': {'title': 'Local strategy'},
                        'opers': [{'name': 'A', 'skill': 1}],
                        'actions': [{'type': 'SkillDaemon'}]}
        self.write()

    def write(self, content=None):
        self.path.write_text(json.dumps(self.content if content is None else content))

    def client(self):
        return LocalCopilotClient(self.catalog, self.path, snapshot=self.snapshot)

    def test_source_is_local_and_exact_bytes_are_preserved_without_network(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network')):
            client = self.client()
            query = client.query('S-1')
            self.assertEqual(query['total'], 1)
            self.assertEqual(query['source']['kind'], 'local')
            self.assertEqual(query['source']['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
            self.assertEqual(self.snapshot.read_bytes(), self.path.read_bytes())
            fetched = client.get(query['candidates'][0]['id'], stage='S-1')
            self.assertEqual(fetched, self.content)
            fetched['actions'].clear()
            self.assertEqual(client.get(1, stage='stage'), self.content)

    def test_changed_source_or_missing_file_cannot_execute_old_snapshot(self):
        client = self.client()
        client.query('S-1')
        self.path.write_bytes(self.path.read_bytes() + b'\n')
        with self.assertRaises(PrtsError):
            client.get(1, stage='S-1')
        self.path.unlink()
        with self.assertRaises(PrtsError):
            client.get(1, stage='S-1')

    def test_wrong_stage_and_candidate_are_rejected(self):
        client = self.client()
        for identity in (0, 2, True):
            with self.assertRaises(PrtsError):
                client.get(identity, stage='S-1')
        self.catalog.aliases['wrong'] = {'other'}
        with self.assertRaises(PrtsError) as raised:
            client.query('wrong')
        self.assertEqual(raised.exception.category, 'stage_mismatch')

    def test_invalid_full_content_and_oversized_file_are_rejected(self):
        bad = [[], dict(self.content, actions='bad'), dict(self.content, actions=[1])]
        for content in bad:
            with self.subTest(content=content):
                self.write(content)
                with self.assertRaises(PrtsError):
                    self.client()
                self.assertFalse(self.snapshot.exists())
        self.path.write_bytes(b' ' * (LocalCopilotClient.MAX_BYTES + 1))
        with self.assertRaises(PrtsError):
            self.client()

    def test_operator_and_difficulty_validation_is_shared_with_remote_source(self):
        for changes in ({'difficulty': 4}, {'opers': 'bad'}, {'doc': {'title': ''}},
                        {'stage_name': 'wrong'}):
            with self.subTest(changes=changes):
                self.write(dict(self.content, **changes))
                client = self.client()
                with self.assertRaises(PrtsError):
                    client.query('S-1')
                self.snapshot.unlink()

    def test_local_source_conflicts_stop_before_device_and_query(self):
        for options in ({'copilot_id': 1}, {'exclude_copilot_ids': [2]},
                        {'acceptance_failure': True}, {'prove_capability': True}):
            with self.subTest(options=options), \
                    patch('maa_planner.copilot_run.load_policy', return_value={}), \
                    patch('maa_planner.copilot_run.device') as device, \
                    patch('maa_planner.copilot_run.PrtsCopilotClient') as remote, \
                    redirect_stderr(io.StringIO()):
                with self.assertRaises(ExperimentError):
                    experiment(self.root, 'S-1', None, copilot_file=self.path, **options)
                device.assert_not_called()
                remote.assert_not_called()
