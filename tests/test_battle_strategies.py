"""Alternative search, shared constraints and real spawn-process isolation."""
import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from maa_planner.battle_generator import prepare_problem
from maa_planner.battle_portfolio import compare_plans
from maa_planner.battle_search import (Formation, Placement, assess_formation,
                                     matrix_contact_damage, normal_fire_deficits)
from maa_planner.battle_solver import BattleSolverError
from maa_planner.battle_strategies import SearchBudget, SearchResult, get_strategy, run_search
from maa_planner.util import canonical_json, sha256_bytes
from tests import test_battle_generator as fixture


class StrategyTests(unittest.TestCase):
    setUp = fixture.GeneratorTests.setUp
    generate = fixture.GeneratorTests.generate

    def research(self):
        return {'schema': 1, 'graph_sha256': sha256_bytes(canonical_json(self.graph)),
                'mechanics': {}, 'sources': ['synthetic fixture']}

    def prepare(self):
        with patch('maa_planner.battle_generator._profiles', return_value=(self.profiles, [])):
            return prepare_problem(self.graph, self.box, self.catalog, self.chars, {}, {},
                                   research=self.research())[0]

    def compare(self, **kwargs):
        with patch('maa_planner.battle_generator._profiles', return_value=(self.profiles, [])), \
                patch('maa_planner.battle_generator.evaluate_plan', return_value={'proof_status': 'unproven'}):
            return compare_plans(self.graph, self.box, self.catalog, self.chars, {}, {},
                                 research=self.research(), **kwargs)

    def dead_end(self):
        # Greedy's first blocker cell (2,1) has no possible medic coverage;
        # cell (3,1) is feasible. These are map facts, not injected exceptions.
        self.profiles[0]['normal_offsets'] = [[0, 0]]
        self.profiles[0]['skill_offsets'] = [[0, 0]]
        self.profiles[0]['row']['matchups'] = []
        self.profiles[-1]['normal_offsets'] = [[0, 0], [0, 1], [1, 0]]
        self.profiles[-1]['skill_offsets'] = self.profiles[-1]['normal_offsets']
        for tile in self.graph['map']['tiles']:
            if tile['y'] != 1 and (tile['x'], tile['y']) not in ((3, 0), (4, 0)):
                tile['buildable'] = 'NONE'

    def test_backtracking_recovers_a_real_greedy_dead_end_within_budget(self):
        self.dead_end()
        with self.assertRaisesRegex(BattleSolverError, 'healing coverage'):
            self.generate()
        recovered = self.generate(strategy='backtracking', max_candidates=4)
        blocker = next(c for c in recovered['generation']['choices'] if c['role'] == 'blocker')
        self.assertIn(blocker['location'], [[3, 1], [4, 1]])
        self.assertLessEqual(recovered['generation']['search']['attempted'], 4)
        with self.assertRaisesRegex(BattleSolverError, 'within backtracking budget'):
            self.generate(strategy='backtracking', max_candidates=1)

    def test_backtracking_is_deterministic_and_cannot_reduce_common_objective(self):
        baseline = self.generate()
        alternative = self.generate(strategy='backtracking')
        self.assertEqual(alternative, self.generate(strategy='backtracking'))
        from maa_planner.battle_search import objective
        self.assertGreaterEqual(objective(alternative['generation']['metrics']),
                                objective(baseline['generation']['metrics']))

    def test_one_failure_is_preserved_without_hiding_other_candidate(self):
        self.dead_end()
        result = self.compare(max_candidates=4)
        rows = result['comparison']['results']
        self.assertEqual([r['status'] for r in rows], ['failed', 'candidate'])
        self.assertEqual(rows[0]['category'], 'infeasible')
        self.assertEqual(result['comparison']['ranking'], ['backtracking'])
        self.assertNotIn('private-fixture', str(result))

    def test_spawn_processes_match_sequential_candidates_and_metrics(self):
        serial, parallel = self.compare(workers=1), self.compare(workers=2)
        self.assertEqual(serial['candidates'], parallel['candidates'])
        self.assertEqual(serial['comparison']['ranking'], parallel['comparison']['ranking'])
        self.assertEqual(parallel['comparison']['workers'], 2)
        for a, b in zip(serial['comparison']['results'], parallel['comparison']['results']):
            self.assertEqual(a['metrics'], b['metrics'])
            self.assertEqual(a['status'], 'candidate')

    def test_worker_inputs_are_derived_and_copied_even_when_sequential(self):
        problem = self.prepare()
        original = copy.deepcopy(problem)
        baseline = get_strategy('greedy')

        def mutate(problem, budget):
            problem.profiles.clear()
            raise RuntimeError('synthetic strategy defect')

        with patch.object(baseline, 'solve', side_effect=mutate):
            result = self.compare()
            self.assertEqual(result['comparison']['results'][0]['category'], 'strategy_error')
            self.assertEqual(result['comparison']['results'][1]['status'], 'candidate')
            with self.assertRaises(RuntimeError):
                run_search(problem, 'greedy', SearchBudget())
        self.assertEqual(problem, original)
        self.assertFalse(hasattr(problem, 'account_id'))

    def test_common_gate_rejects_strategy_proposals_with_unhealed_blockers(self):
        problem = self.prepare()
        formation = Formation((Placement(0, (3, 1), 'Right', 'blocker'),))
        with self.assertRaisesRegex(BattleSolverError, 'healing coverage'):
            assess_formation(problem, formation)
        fake = SearchResult('greedy', formation, {})
        with patch('maa_planner.battle_portfolio.run_search', return_value=fake):
            result = self.compare(strategies=['greedy'])
        self.assertFalse(result['candidates'])
        self.assertEqual(result['comparison']['results'][0]['category'], 'validation_error')

    def test_budgets_strategy_ids_and_duplicates_are_strict(self):
        for options in ({'max_candidates': 0}, {'max_candidates': True}, {'branch_width': 9},
                        {'strategies': ['new-unknown']}, {'strategies': ['greedy', 'greedy']},
                        {'workers': 0}):
            with self.subTest(options=options), self.assertRaises(BattleSolverError):
                self.compare(**options)

    def test_matrix_normal_contact_checks_damage_attack_phase_speed_and_gaps(self):
        problem = self.prepare()
        problem.mechanics['enemy']['kind'] = 'matrix'
        problem = replace(problem, enemy_attributes={'enemy': {'maxHp': 1000, 'moveSpeed': 1}},
                          move_multiplier=1)
        caster = problem.profiles[1]
        caster['row']['matchups'][0]['normal_attack_damage'] = 100
        selected = [(caster, (2, 0), 'Down')]
        # Three contiguous tiles allow two guaranteed attacks after reserving
        # a full initial attack interval; a boundary tie is not a free hit.
        self.assertEqual(matrix_contact_damage(problem, *selected[0], 0, 'enemy'), 200)
        self.assertIn(0, normal_fire_deficits(problem, selected, {0}))
        # Huge skill DPS must not hide an insufficient normal attack.
        caster['row']['matchups'][0]['rotation']['average_dps'] = 100000
        self.assertIn(0, normal_fire_deficits(problem, selected, {0}))
        caster['row']['matchups'][0]['normal_attack_damage'] = 500
        self.assertFalse(normal_fire_deficits(problem, selected, {0}))
        problem.enemy_attributes['enemy']['moveSpeed'] = 2
        self.assertIn(0, normal_fire_deficits(problem, selected, {0}))
        # Two disconnected one-tile windows cannot be merged into two attacks.
        problem.enemy_attributes['enemy']['moveSpeed'] = 1
        caster['normal_offsets'] = [[0, 0], [2, 0]]
        self.assertEqual(matrix_contact_damage(problem, caster, (0, 1), 'Right', 0, 'enemy'), 0)
        problem = replace(problem, move_multiplier=None)
        with self.assertRaisesRegex(BattleSolverError, 'known movement'):
            matrix_contact_damage(problem, caster, (0, 1), 'Right', 0, 'enemy')

    def test_crossing_heavy_enemy_requires_independent_fire_for_the_other_stream(self):
        problem = self.prepare()
        # A heavy enemy crosses the normal stream at the defender. Its source
        # route need not share the normal stream's goal or route ID.
        problem.paths[1] = {'cells': {(3, 0), (3, 1), (3, 2)},
                            'terminal': [(3, 0), (3, 1)], 'goal': (3, 2)}
        problem.spawns.append({'enemy_id': 'heavy', 'route_index': 1, 'count': 1})
        problem.mechanics['heavy'] = {'block_cost': 2}
        problem.profiles[-1]['normal_offsets'] = [[x, y] for x in range(-6, 7)
                                                 for y in range(-3, 4)]
        def formation(caster):
            return Formation((Placement(0, (3, 1), 'Right', 'blocker'),
                              Placement(1, caster, 'Right', 'damage'),
                              Placement(2, (5, 2), 'Right', 'healer')))
        # Basic geometry and healing alone used to admit this formation.
        with self.assertRaisesRegex(BattleSolverError, 'independent normal DPS'):
            assess_formation(problem, formation((0, 0)))
        metrics, _ = assess_formation(problem, formation((2, 0)))
        self.assertEqual(metrics['required_normal_fire_routes'], [0, 1])
        # Expanded skill range cannot stand in for permanent coverage.
        problem.profiles[1]['skill_offsets'] = [[x, y] for x in range(-6, 7)
                                               for y in range(-3, 4)]
        with self.assertRaisesRegex(BattleSolverError, 'independent normal DPS'):
            assess_formation(problem, formation((0, 0)))
        # A one-slot crossing enemy does not alone saturate this two-block unit.
        problem.mechanics['heavy']['block_cost'] = 1
        metrics, _ = assess_formation(problem, formation((0, 0)))
        self.assertEqual(metrics['required_normal_fire_routes'], [])

    def test_comparison_deduplicates_identical_outputs_without_deleting_evidence(self):
        result = self.compare(max_candidates=1)
        rows = result['comparison']['results']
        self.assertEqual(set(result['candidates']), {'greedy', 'backtracking'})
        self.assertEqual(rows[1]['duplicate_of'], 'greedy')
        self.assertEqual(rows[0]['copilot_sha256'], rows[1]['copilot_sha256'])

    def test_shared_preflight_rejects_unknown_mechanics_for_all_strategies(self):
        self.graph['enemies']['enemy']['skills'] = [{'unknown': True}]
        with self.assertRaisesRegex(BattleSolverError, 'enemy adapters'):
            self.compare(workers=2)

    def test_only_primary_control_can_affect_common_comparison_metrics(self):
        for profile in self.profiles:
            profile['row']['conditional_control_scenarios'] = [
                {'credited_to_primary_estimate': False, 'effects': {'movement_delay_equivalent_seconds': 1000}}]
        baseline = self.generate()['generation']['metrics']
        self.assertEqual(baseline['total_control_contact_score'], 0)
        self.profiles[1]['row']['matchups'][0]['rotation']['effects'] = {
            'movement_delay_equivalent_seconds': 10}
        for strategy in ('greedy', 'backtracking'):
            result = self.generate(strategy=strategy)['generation']['metrics']
            self.assertGreater(result['total_control_contact_score'], 0)


if __name__ == '__main__':
    unittest.main()
