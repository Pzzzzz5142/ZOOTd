"""CLI acceptance using synthetic sources; no account, network or device access."""
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from maa_planner.battle_cli import main
from maa_planner.battle_data import parse_battle
from maa_planner.copilot_local import LocalCopilotClient
from maa_planner.operator_box import Operator, OperatorBox, SkillProgress
from maa_planner.prts import StageCatalog
from maa_planner.util import atomic_write_json, sha256_bytes
from tests.test_battle_data import fixture


class BattleCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.revision = 'a' * 40
        self.uid = 'Official:synthetic-private-uid'
        level, enemies, identity, sources = fixture()
        sources['navigation'] = {'revision': self.revision, 'sha256': 'b' * 64}
        graph = parse_battle(level, enemies, identity=identity, sources=sources)
        self.graph = self.root / 'graph.json'
        atomic_write_json(self.graph, graph)
        self.plan = self.root / 'plan.json'
        self.plan_value = {
            'schema_version': 1, 'stage_id': identity['stage_id'], 'title': 'Synthetic offline plan',
            'mechanics_acknowledged': graph['mechanics']['unknown'],
            'placements': [
                {'name': 'Synthetic Guard', 'skill': 1, 'location': [1, 0], 'direction': 'Right',
                 'requirements': {'elite': 2, 'skill_level': 10}},
                {'name': 'Synthetic Medic', 'skill': 1, 'location': [0, 1], 'direction': 'Up'},
            ],
        }
        atomic_write_json(self.plan, self.plan_value)
        self.output = self.root / 'copilot.json'
        self.box = self.root / 'var/state/operator-box.json'
        box = OperatorBox('synthetic-fixture', '2026-10-09T12:00:00+08:00', self.uid, {
            'char_001_fixture': Operator('char_001_fixture', 2, 80, 1, 7,
                                         {'fixture_s1': SkillProgress(3)}, {}),
            'char_002_fixture': Operator('char_002_fixture', 1, 50, 1, 7,
                                         {'fixture_m1': SkillProgress(0)}, {}),
        })
        atomic_write_json(self.box, box.to_dict())
        self.static_directory = self.root / 'var/cache/battle-data' / self.revision / 'static'
        self.static_directory.mkdir(parents=True)
        characters = {
            'char_001_fixture': {'name': 'Synthetic Guard', 'position': 'MELEE',
                                 'skills': [{'skillId': 'fixture_s1'}]},
            'char_002_fixture': {'name': 'Synthetic Medic', 'position': 'RANGED',
                                 'skills': [{'skillId': 'fixture_m1'}]},
        }
        equips = {'charEquip': {}, 'equipDict': {}}
        for name, data in (('character_table', characters), ('uniequip_table', equips)):
            path = self.static_directory / (name + '.json')
            # Whitespace is intentional: cache provenance hashes received bytes.
            raw = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
            path.write_bytes(raw)
            self.write_receipt(path, raw)
        self.installed = self.root / 'var/data/MaaResource/resource/battle_data.json'
        atomic_write_json(self.installed, {'chars': {
            key: {'name': value['name']} for key, value in characters.items()}})
        # A stale base file ensures static inputs use the promoted layer.
        atomic_write_json(self.root / 'var/data/resource/battle_data.json', {'chars': {}})

    def write_receipt(self, path, raw):
        atomic_write_json(path.with_name(path.name + '.source.json'), {
            'url': f'https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/{self.revision}'
                   f'/zh_CN/gamedata/excel/{path.name}',
            'sha256': sha256_bytes(raw), 'fetched_at': 100,
        })

    def args(self, command, extra=()):
        value = ['--project-root', str(self.root), command, '--graph', str(self.graph),
                 '--plan', str(self.plan), '--offline']
        if command == 'compile':
            value += ['--output', str(self.output)]
        else:
            value += ['--copilot', str(self.output)]
        return value + list(extra)

    def run_cli(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Unexpected network')):
            code = main(arguments)
        return code, out.getvalue(), err.getvalue()

    def compile(self):
        code, out, err = self.run_cli(self.args('compile'))
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_compilation_is_readable_by_existing_local_copilot_client(self):
        report = self.compile()
        self.assertEqual(report['proof_status'], 'unproven')
        self.assertEqual(report['box_validation'], 'verified')
        stages = StageCatalog([{'stageId': 'synthetic_01', 'code': 'XX-1',
                                'levelId': 'Activities/synthetic/level_synthetic_01'}])
        snapshot = self.root / 'local-snapshot.json'
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Unexpected network')):
            client = LocalCopilotClient(stages, self.output, snapshot=snapshot)
            candidate = client.query('XX-1')['candidates'][0]
            content = client.get(candidate['id'], stage='XX-1')
        self.assertEqual(content['difficulty'], 1)
        self.assertEqual(content['actions'][0]['location'], [1, 0])
        self.assertEqual(snapshot.read_bytes(), self.output.read_bytes())
        self.assertEqual(report['inputs']['static']['maa_battle_data']['path'], str(self.installed))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_compile_and_validate_reports_do_not_expose_account_identity(self):
        report = self.compile()
        code, out, err = self.run_cli(self.args('validate'))
        self.assertEqual(code, 0, err)
        analysis = self.output.with_suffix('.analysis.json').read_text()
        for value in (json.dumps(report), out, err, analysis, self.output.read_text()):
            self.assertNotIn(self.uid, value)
            self.assertNotIn('account_id', value)
        self.assertEqual(report['inputs']['box']['sha256'], sha256_bytes(self.box.read_bytes()))
        self.assertEqual(report['inputs']['plan_sha256'], sha256_bytes(self.plan.read_bytes()))

    def test_mutated_candidate_cannot_reuse_another_plans_mechanics_acknowledgements(self):
        self.compile()
        content = json.loads(self.output.read_bytes())
        # Both tiles are legally MELEE; this must be rejected by plan binding,
        # not by unrelated placement or progression checks.
        content['actions'][0]['location'] = [1, 1]
        atomic_write_json(self.output, content)
        code, out, err = self.run_cli(self.args('validate'))
        self.assertEqual(code, 1, 'A changed candidate must not inherit the original plan acknowledgement')
        self.assertFalse(out)
        self.assertEqual(json.loads(err)['status'], 'error')

    def test_validate_requires_explicit_plan_and_rejects_bad_plan_shape(self):
        self.compile()
        no_plan = self.args('validate')
        index = no_plan.index('--plan')
        del no_plan[index:index + 2]
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(no_plan)
        self.assertEqual(raised.exception.code, 2)
        for invalid in ([], dict(self.plan_value, schema_version=99),
                        dict(self.plan_value, stage_id='synthetic_02')):
            with self.subTest(invalid=invalid):
                atomic_write_json(self.plan, invalid)
                code, out, err = self.run_cli(self.args('validate'))
                self.assertEqual(code, 1)
                self.assertFalse(out)
                self.assertEqual(json.loads(err)['status'], 'error')

    def test_unknown_box_progression_and_invalid_box_types_do_not_emit_candidate(self):
        original = json.loads(self.box.read_bytes())
        for field, value in (('elite', None), ('level', True), ('main_skill_level', '7')):
            with self.subTest(field=field):
                box = copy.deepcopy(original)
                box['operators']['char_001_fixture'][field] = value
                atomic_write_json(self.box, box)
                code, out, err = self.run_cli(self.args('compile'))
                self.assertEqual(code, 1)
                self.assertFalse(self.output.exists())
                self.assertNotIn(self.uid, out + err)

    def test_offline_static_source_tampering_and_wrong_revision_receipt_are_rejected(self):
        path = self.static_directory / 'character_table.json'
        raw = path.read_bytes()
        receipt = path.with_name(path.name + '.source.json')
        evidence = json.loads(receipt.read_bytes())
        for kind in ('bytes', 'revision'):
            with self.subTest(kind=kind):
                path.write_bytes(raw)
                atomic_write_json(receipt, evidence)
                if kind == 'bytes':
                    path.write_bytes(raw + b' ')
                else:
                    changed = dict(evidence, url=evidence['url'].replace(self.revision, 'c' * 40))
                    atomic_write_json(receipt, changed)
                code, out, err = self.run_cli(self.args('compile'))
                self.assertEqual(code, 1)
                self.assertFalse(self.output.exists())
                self.assertEqual(json.loads(err)['status'], 'error')

    def test_offline_missing_static_sources_do_not_fall_back_to_network(self):
        (self.static_directory / 'uniequip_table.json.source.json').unlink()
        code, out, err = self.run_cli(self.args('compile'))
        self.assertEqual(code, 1)
        self.assertFalse(out)
        self.assertFalse(self.output.exists())
        self.assertIn('not cached', json.loads(err)['message'])

    def test_native_deployment_timeout_is_preserved_in_compiled_candidate(self):
        plan = copy.deepcopy(self.plan_value)
        plan['placements'][0]['timeout'] = 5000
        atomic_write_json(self.plan, plan)
        self.compile()
        self.assertEqual(json.loads(self.output.read_bytes())['actions'][0]['timeout'], 5000)

    def test_native_integer_overflow_is_refused_before_emitting_candidate(self):
        for field, value in (('pre_delay', 2 ** 31), ('costs', 2 ** 31),
                             ('cost_changes', -(2 ** 31) - 1), ('elapsed_time', 2 ** 31),
                             ('cooling', 2 ** 31)):
            with self.subTest(field=field):
                plan = copy.deepcopy(self.plan_value)
                plan['placements'][0][field] = value
                atomic_write_json(self.plan, plan)
                code, out, err = self.run_cli(self.args('compile'))
                self.assertEqual(code, 1, 'MAA integer fields must fit the native signed int')
                self.assertFalse(out)
                self.assertFalse(self.output.exists())
                self.assertEqual(json.loads(err)['status'], 'error')

    def test_public_shell_entrypoint_dispatches_offline_compile_and_validation(self):
        # Run the real entrypoint in an isolated root so host.local.env and the
        # user's private environment files cannot be sourced by this test.
        repository = Path(__file__).resolve().parents[1]
        entrypoint = self.root / 'bin/zootd'
        entrypoint.parent.mkdir()
        entrypoint.write_bytes((repository / 'bin/zootd').read_bytes())
        entrypoint.chmod(0o700)
        environment = dict(os.environ, PYTHONPATH=str(repository), PYTHONDONTWRITEBYTECODE='1')
        for command in ('compile', 'validate'):
            arguments = self.args(command)[2:]
            process = subprocess.run([str(entrypoint), 'battle-solver', *arguments],
                                     cwd=self.root, env=environment, capture_output=True,
                                     text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(process.stdout)
            self.assertEqual(report['box_validation'], 'verified')
            self.assertNotIn(self.uid, process.stdout + process.stderr)


if __name__ == '__main__':
    unittest.main()
