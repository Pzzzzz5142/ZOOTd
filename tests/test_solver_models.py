from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from maa_planner.solver_models import build_catalog

REVISION = 'a' * 40


def write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def character_table():
    return {'char_test': {
        'name': '测试干员', 'rarity': 'TIER_6', 'profession': 'WARRIOR', 'position': 'MELEE',
        'skills': [{'skillId': 'skchr_clemnt_3', 'unlockCond': {'phase': 'PHASE_0', 'level': 1}}]}}


def skill_table():
    level = {'name': '与海为敌', 'rangeId': '3-2', 'description': '', 'skillType': 'MANUAL',
             'duration': 0., 'spData': {'spType': 'INCREASE_WITH_TIME', 'initSp': 30, 'spCost': 42,
                                        'maxChargeTime': 1},
             'blackboard': [{'key': k, 'value': v} for k, v in {
                 'attack@atk_scale': 2.35, 'attack@max_target': 3., 'trigger_time': 10.}.items()]}
    return {'skchr_clemnt_3': {'levels': [level] * 10}}


def box(operators):
    return {'schema': 1, 'source': 'fixture', 'fetched_at': '2026-10-18T00:00:00Z',
            'account_id': 'fixture', 'operators': operators}


class SolverModelCatalogTests(unittest.TestCase):
    def root(self, *, with_box=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        write(root, f'var/cache/battle-data/{REVISION}/static/character_table.json', character_table())
        write(root, f'var/cache/battle-data/{REVISION}/static/skill_table.json', skill_table())
        if with_box:
            write(root, 'var/state/operator-box.json', box({'char_test': {
                'id': 'char_test', 'elite': 2, 'level': 70, 'potential': 1,
                'main_skill_level': 7, 'skills': {'skchr_clemnt_3': {'mastery': 3}},
                'modules': {}, 'favor_percent': 100, 'default_equip_id': None}}))
        return root

    def test_catalog_lists_modeled_operators_with_box_training(self):
        result = build_catalog(self.root())
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['revision'], REVISION)
        self.assertEqual(result['counts'], {
            'modeled_operators': 1, 'modeled_skills': 1, 'box_operators': 1,
            'modeled_in_box': 1, 'unmodeled_in_box': 0})
        operator = result['operators'][0]
        self.assertEqual((operator['name'], operator['profession'], operator['in_box']),
                         ('测试干员', '近卫', True))
        skill = operator['skills'][0]
        self.assertEqual((skill['slot'], skill['name'], skill['level'], skill['level_source']),
                         (1, '与海为敌', 10, 'box'))
        self.assertEqual(skill['mode'], 'ammo')
        self.assertIsNotNone(skill['note'])
        self.assertIn('弹药 10', skill['summary'])
        self.assertEqual(skill['unknown'], ['clemnt:erosion_burst_ammo_and_elemental_damage_not_simulated'])

    def test_missing_box_is_explicit_instead_of_zero(self):
        result = build_catalog(self.root(with_box=False))
        self.assertEqual(result['state'], 'ready')
        self.assertFalse(result['box_available'])
        self.assertIsNone(result['operators'][0]['in_box'])
        self.assertIsNone(result['counts']['box_operators'])
        self.assertEqual(result['operators'][0]['skills'][0]['level_source'], 'model')

    def test_missing_static_data_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            result = build_catalog(Path(directory))
        self.assertEqual(result['state'], 'unavailable')
        self.assertIn('battle-solver fetch', result['message'])

    def test_every_modeled_skill_has_a_note(self):
        cache = Path(__file__).resolve().parents[1] / 'var/cache/battle-data'
        if not any(cache.glob('*/static/character_table.json')):
            self.skipTest('no cached game data')
        result = build_catalog(Path(__file__).resolve().parents[1])
        self.assertEqual(result['state'], 'ready')
        for operator in result['operators']:
            for skill in operator['skills']:
                with self.subTest(skill=skill['skill_id']):
                    self.assertIsNotNone(skill['note'])


if __name__ == '__main__':
    unittest.main()
