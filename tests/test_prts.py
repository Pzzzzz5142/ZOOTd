"""Synthetic protocol fixtures; never connects to PRTS or a device."""
import io
import http.client
import json
import unittest
import urllib.error
from unittest.mock import patch

from maa_planner.prts import PrtsCopilotClient, PrtsError, StageCatalog
from maa_planner.prts_cli import main


STAGES = [{"stageId": "main_01-07", "code": "1-7"},
          {"stageId": "main_01-08", "code": "1-8"},
          {"stageId": "event_old", "code": "EV-1"},
          {"stageId": "event_new", "code": "EV-1"}]
CONTENT = {"stage_name": "main_01-07", "doc": {"title": "Synthetic fixture"},
           "opers": [{"name": "Fixture A", "skill": 2,
                      "requirements": {"elite": 2, "future_requirement": {"unknown": 7}}}],
           "groups": [{"name": "healer", "opers": [{"name": "Fixture B"}]}]}


def row(content=None):
    return {"id": 12, "type": "PRTS", "available": True, "hot_score": 1.5,
            "content": json.dumps(CONTENT if content is None else content)}


def page(rows=None):
    return {"status_code": 200, "data": {"page": 1, "has_next": False, "total": 1,
            "data": [row()] if rows is None else rows}}


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return io.BytesIO(self.response if isinstance(self.response, bytes)
                          else json.dumps(self.response).encode())


class PrtsTests(unittest.TestCase):
    def client(self, response):
        self.opener = Opener(response)
        return PrtsCopilotClient(StageCatalog(STAGES), opener=self.opener)

    def error(self, category, call):
        with self.assertRaises(PrtsError) as caught:
            call()
        self.assertEqual(caught.exception.category, category)

    def test_lightweight_query_normalizes_without_get(self):
        result = self.client(page()).query("1-7")
        candidate = result['candidates'][0]
        self.assertEqual(candidate['stage'], 'main_01-07')
        self.assertEqual(candidate['groups'][0]['operators'][0]['skill'], None)
        self.assertEqual(candidate['operators'][0]['requirements']['future_requirement'], {'unknown': 7})
        self.assertNotIn('actions', candidate)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertIn('level_keyword=main_01-07', self.opener.calls[0][0])
        self.assertNotIn('/get/', self.opener.calls[0][0])

    def test_stage_aliases_and_ambiguity(self):
        catalog = StageCatalog(STAGES)
        self.assertEqual(catalog.resolve('MAIN_01-07'), 'main_01-07')
        self.error('stage_identity', lambda: catalog.resolve('EV-1'))
        self.error('stage_identity', lambda: catalog.resolve('unknown'))
        client = self.client(page())
        self.error('stage_identity', lambda: client.query('unknown'))
        self.assertFalse(self.opener.calls)

    def test_candidate_stage_mismatch_and_unknown(self):
        for stage in ('1-8', 'foreign'):
            content = dict(CONTENT, stage_name=stage)
            self.error('stage_mismatch', lambda: self.client(page([row(content)])).query('1-7'))

    def test_candidate_code_alias_is_checked(self):
        result = self.client(page([row(dict(CONTENT, stage_name='1-7'))])).query('main_01-07')
        self.assertEqual(result['candidates'][0]['stage'], 'main_01-07')

    def test_selected_full_copilot(self):
        content = dict(CONTENT, actions=[{'type': 'Deploy', 'name': 'Fixture A'}])
        result = self.client({'status_code': 200, 'data': row(content)}).get(12, stage='1-7')
        self.assertEqual(result, content)
        self.assertEqual(self.opener.calls[0][0], 'https://prts.maa.plus/copilot/get/12')

    def test_full_copilot_rechecks_stage_id_and_actions(self):
        for content, identity, category in (
            (dict(CONTENT, stage_name='1-8', actions=[{}]), 12, 'stage_mismatch'),
            (dict(CONTENT, actions=[{}]), 13, 'schema_error'),
            (CONTENT, 12, 'schema_error'),
            (dict(CONTENT, actions=[]), 12, 'schema_error'),
        ):
            payload = row(content)
            payload['id'] = identity
            self.error(category, lambda: self.client({'status_code': 200, 'data': payload}).get(12, stage='1-7'))

    def test_empty_result(self):
        self.error('empty_result', lambda: self.client(page([])).query('1-7'))

    def test_network_not_found_and_service_errors(self):
        for response, category in (
            (TimeoutError(), 'network'),
            (http.client.IncompleteRead(b'partial'), 'network'),
            (urllib.error.URLError('offline'), 'network'),
            (urllib.error.HTTPError('url', 404, 'missing', {}, None), 'copilot_not_found'),
            ({'status_code': 404}, 'copilot_not_found'),
            ({'status_code': 503}, 'network'),
        ):
            self.error(category, lambda: self.client(response).get(12, stage='1-7'))

    def test_strict_json_and_size(self):
        for response in (b'not json', b'{"status_code":200,"status_code":200}',
                         b'{"status_code":NaN}', b'{"data":1e999}', b'[]', b'{}'):
            self.error('schema_error', lambda: self.client(response).query('1-7'))
        client = self.client(b' ' * 30)
        client.MAX_BYTES = 20
        self.error('schema_error', lambda: client.query('1-7'))

    def test_malformed_candidates_fail_closed(self):
        variants = [dict(row(), id=True), dict(row(), available=False), dict(row(), type='SSS'),
                    dict(row(), content='{}'), dict(row(), hot_score='hot')]
        for key, value in (('opers', None), ('groups', {}), ('difficulty', True),
                           ('opers', [{'name': 'A', 'skill': 4}]),
                           ('opers', [{'name': 'A', 'role': {}}]),
                           ('opers', [{'name': 'A', 'requirements': []}]),
                           ('groups', [{'name': 'A', 'opers': []}])):
            variants.append(row(dict(CONTENT, **{key: value})))
        for variant in variants:
            with self.subTest(variant=variant):
                self.error('schema_error', lambda: self.client(page([variant])).query('1-7'))

    def test_pagination_and_duplicate_ids(self):
        self.error('schema_error', lambda: self.client(page([row(), row()])).query('1-7'))
        for key, value in (('page', 2), ('has_next', 'true'), ('total', -1), ('data', {})):
            response = page()
            response['data'][key] = value
            self.error('schema_error', lambda: self.client(response).query('1-7'))
        client = self.client(page())
        self.error('input', lambda: client.query('1-7', limit=51))
        self.error('input', lambda: client.get(True, stage='1-7'))
        self.assertFalse(self.opener.calls)

    def test_cli_dispatch_and_failure(self):
        with patch('maa_planner.prts_cli.StageCatalog.load', return_value=StageCatalog(STAGES)), \
             patch('maa_planner.prts_cli.PrtsCopilotClient') as factory, \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            factory.return_value.query.return_value = {'candidates': []}
            self.assertEqual(main(['--project-root', '/unused', 'copilot-query', '1-7', '--limit', '3']), 0)
            factory.return_value.query.assert_called_once_with('1-7', page=1, limit=3)
            self.assertEqual(json.loads(output.getvalue()), {'candidates': []})
        with patch('maa_planner.prts_cli.StageCatalog.load', side_effect=PrtsError('stage_identity', 'missing')), \
             patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertEqual(main(['--project-root', '/unused', 'copilot-get', '12', '--stage', '1-7']), 1)
            self.assertEqual(json.loads(output.getvalue())['category'], 'stage_identity')


if __name__ == '__main__':
    unittest.main()
