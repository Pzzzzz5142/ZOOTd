"""Transport refusal tests; synthetic HTTP errors, no network or Git process."""
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, patch

from maa_planner.navigation_catalog import _request, REVISION_URL
from maa_planner.prts import PrtsError


class NavigationSourceTests(unittest.TestCase):
    def error(self, code, headers=None, url=REVISION_URL):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(url, code, 'fixture', headers or {}, None)
        return patch('maa_planner.navigation_catalog.urllib.request.build_opener', return_value=opener)

    def test_rate_limit_resolves_exact_branch_with_git_then_retains_normal_pinned_table_flow(self):
        for code, headers in [(403, {'X-RateLimit-Remaining': '0'}), (429, {})]:
            with self.subTest(code=code), self.error(code, headers), patch(
                    'maa_planner.navigation_catalog.subprocess.run',
                    return_value=Mock(stdout='a' * 40 + '\trefs/heads/master\n')) as run:
                self.assertEqual(_request(REVISION_URL), {'sha': 'a' * 40})
                self.assertEqual(run.call_args.args[0][-1], 'refs/heads/master')
                self.assertEqual(run.call_args.kwargs['timeout'], 40)

    def test_table_failure_and_non_rate_limit_403_never_change_source(self):
        table = 'https://raw.githubusercontent.com/example/table.json'
        for url, code, headers in [(table, 403, {'X-RateLimit-Remaining': '0'}),
                                   (REVISION_URL, 403, {}), (REVISION_URL, 401, {})]:
            with self.subTest(url=url, code=code), self.error(code, headers, url), patch(
                    'maa_planner.navigation_catalog.subprocess.run') as run:
                with self.assertRaises(PrtsError):
                    _request(url)
                run.assert_not_called()

    def test_wrong_ref_ambiguous_output_missing_git_or_timeout_cannot_refresh(self):
        for output in ['b' * 40 + '\trefs/heads/other\n',
                       'a' * 40 + '\trefs/heads/master\n' + 'b' * 40 + '\trefs/heads/master\n', '']:
            with self.subTest(output=output), self.error(429), patch(
                    'maa_planner.navigation_catalog.subprocess.run', return_value=Mock(stdout=output)):
                with self.assertRaises(PrtsError):
                    _request(REVISION_URL)
        for error in [FileNotFoundError(), subprocess.TimeoutExpired('git', 40)]:
            with self.subTest(error=error), self.error(429), patch(
                    'maa_planner.navigation_catalog.subprocess.run', side_effect=error):
                with self.assertRaises(PrtsError):
                    _request(REVISION_URL)
