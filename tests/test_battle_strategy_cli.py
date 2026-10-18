"""Full public CLI with synthetic pinned tables and real process workers."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from maa_planner.operator_box import Operator, OperatorBox, SkillProgress
from maa_planner.util import atomic_write_json, canonical_json, sha256_bytes
from tests.test_battle_generator import problem


class StrategyCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        repository = Path(__file__).resolve().parents[1]
        self.environment = dict(os.environ, PYTHONPATH=str(repository), PYTHONDONTWRITEBYTECODE='1')
        self.entrypoint = self.root / 'bin/zootd'
        self.entrypoint.parent.mkdir()
        self.entrypoint.write_bytes((repository / 'bin/zootd').read_bytes())
        self.entrypoint.chmod(0o700)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '--allow-empty',
                        '-m', 'Synthetic source identity'], check=True, capture_output=True)
        revision = 'a' * 40
        graph = problem()
        graph['sources'] = {'navigation': {'revision': revision}}
        graph['spawns'][0].update(wave=0, fragment=0, pre_delay=0, interval=3)
        self.graph = self.root / 'graph.json'
        atomic_write_json(self.graph, graph)
        self.research = self.root / 'research.json'
        atomic_write_json(self.research, {'schema': 1,
            'graph_sha256': sha256_bytes(canonical_json(graph)), 'mechanics': {},
            'sources': ['synthetic fixture, not observed game behavior']})
        chars, skills, operators = {}, {}, {}
        for key, name, profession, position, sid, blackboard in (
            ('char_guard', 'Guard', 'WARRIOR', 'MELEE', 'skchr_huang_2', {'atk': .5, 'def': .5}),
            ('char_caster', 'Caster', 'CASTER', 'RANGED', 'skchr_amgoat_3',
             {'atk': 1., 'base_attack_time': -.2, 'attack@max_target': 3}),
            ('char_medic', 'Medic', 'MEDIC', 'RANGED', 'skchr_agoat2_1', {'atk': .5}),
        ):
            attrs = dict(atk=500, maxHp=2000, **{'def': 300}, cost=10, blockCnt=3,
                         baseAttackTime=1., attackSpeed=100., spRecoveryPerSec=1.)
            phase = {'maxLevel': 80, 'rangeId': 'range', 'attributesKeyFrames': [
                {'level': level, 'data': attrs} for level in (1, 80)]}
            chars[key] = {'name': name, 'position': position, 'profession': profession,
                'skills': [{'skillId': sid, 'unlockCond': {'phase': 'PHASE_1', 'level': 1}}],
                'phases': [phase, phase]}
            level = {'skillType': 'MANUAL', 'duration': 10, 'spData': {
                'spType': 'INCREASE_WITH_TIME', 'initSp': 0, 'spCost': 10, 'maxChargeTime': 1},
                'blackboard': [{'key': k, 'value': v} for k, v in blackboard.items()]}
            skills[sid] = {'levels': [level] * 7}
            operators[key] = Operator(key, 1, 50, 1, 7, {sid: SkillProgress(0)}, {})
        self.box = self.root / 'var/state/operator-box.json'
        atomic_write_json(self.box, OperatorBox('fixture', '2026-10-18', 'private-cli-fixture', operators).to_dict())
        static = self.root / 'var/cache/battle-data' / revision / 'static'
        for name, table in {'character_table': chars, 'uniequip_table': {'charEquip': {}, 'equipDict': {}},
            'skill_table': skills, 'battle_equip_table': {}, 'range_table': {'range': {'grids': [
                {'col': x, 'row': y} for x in range(3) for y in range(-1, 2)]}}}.items():
            path = static / (name + '.json')
            atomic_write_json(path, table)
            atomic_write_json(path.with_name(path.name + '.source.json'), {
                'url': f'https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/{revision}'
                       f'/zh_CN/gamedata/excel/{path.name}',
                'sha256': sha256_bytes(path.read_bytes()), 'fetched_at': 100})
        atomic_write_json(self.root / 'var/data/MaaResource/resource/battle_data.json', {
            'chars': {k: {'name': v['name']} for k, v in chars.items()}})

    def cli(self, command, *extra):
        args = [str(self.entrypoint), 'battle-solver', command]
        if command != 'strategies':
            args += ['--graph', str(self.graph), '--research', str(self.research), '--offline']
        return subprocess.run(args + list(extra), cwd=self.root, env=self.environment,
                              capture_output=True, text=True, timeout=30)

    def test_public_parallel_comparison_records_fixed_inputs_and_private_artifacts(self):
        directory = self.root / 'comparison'
        result = self.cli('compare', '--workers', '2', '--max-candidates', '3', '--output', str(directory))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, json.loads((directory / 'comparison.json').read_bytes()))
        self.assertEqual(report['workers'], 2)
        self.assertEqual(report['proof_status'], 'unproven')
        self.assertNotIn('private-cli-fixture', result.stdout + result.stderr)
        for row in report['results']:
            self.assertEqual(row['status'], 'candidate', row)
            candidate = directory / row['strategy']
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o700)
            generation = json.loads((candidate / 'generation.json').read_bytes())
            self.assertEqual(generation['inputs'], report['inputs'])
            self.assertEqual(generation['strategy']['id'], row['strategy'])
            self.assertIn('--strategies', generation['entrypoint'])
            for key in ('plan', 'copilot'):
                path = candidate / (key + '.json')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(generation[key + '_sha256'],
                                 sha256_bytes(canonical_json(json.loads(path.read_bytes()))))
            self.assertTrue(row['coverage']['operator_unknowns'])
        # Default generate retains the old output layout and the greedy result.
        single = self.root / 'single'
        baseline = self.cli('generate', '--output', str(single))
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assertEqual((single / 'copilot.json').read_bytes(), (directory / 'greedy/copilot.json').read_bytes())
        # Immutable output directories cannot silently overwrite a previous comparison.
        original = (directory / 'comparison.json').read_bytes()
        again = self.cli('compare', '--output', str(directory))
        self.assertEqual(again.returncode, 1)
        self.assertEqual((directory / 'comparison.json').read_bytes(), original)

    def test_discovery_budget_errors_and_all_failed_report(self):
        listed = self.cli('strategies')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([s['id'] for s in json.loads(listed.stdout)['strategies']], ['greedy', 'backtracking'])
        invalid = self.root / 'invalid'
        failed = self.cli('compare', '--max-candidates', '0', '--output', str(invalid))
        self.assertEqual(failed.returncode, 1)
        self.assertFalse(invalid.exists())
        # Removing the only medic makes both searches infeasible; no candidate
        # is emitted, while the comparison and reasons remain reviewable.
        box = json.loads(self.box.read_bytes())
        del box['operators']['char_medic']
        atomic_write_json(self.box, box)
        directory = self.root / 'failed'
        failed = self.cli('compare', '--workers', '2', '--output', str(directory))
        self.assertEqual(failed.returncode, 1, failed.stderr)
        report = json.loads((directory / 'comparison.json').read_bytes())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['ranking'], [])
        self.assertTrue(all(r['status'] == 'failed' for r in report['results']))
        self.assertEqual(list(directory.iterdir()), [directory / 'comparison.json'])


if __name__ == '__main__':
    unittest.main()
