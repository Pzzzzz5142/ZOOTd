"""Synthetic offline matcher matrix; no accounts, network or device calls."""
from dataclasses import replace
import itertools
import unittest

from maa_planner.copilot_matcher import (
    OperatorCatalog, OperatorIdentity, effective_skill_level, match_candidate, rank_candidates,
)
from maa_planner.operator_box import Operator, OperatorBox, SkillProgress, ModuleProgress
from maa_planner.prts import CopilotCandidate


def spec(name='A', skill=3, **requirements):
    return dict(name=name, skill=skill, requirements=requirements)


def candidate(opers=None, groups=None, id=1, **metadata):
    return CopilotCandidate(id, 'stage', 'fixture', opers or [], groups or [], None, metadata)


def group(*names):
    return dict(name='choice', operators=[spec(name) for name in names])


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.catalog = OperatorCatalog([
            OperatorIdentity(name, name, {1: name+'s1', 2: name+'s2', 3: name+'s3'}, {1: name+'x', 2: name+'y'})
            for name in 'ABCD'])
        self.operator = Operator('A', 2, 90, 6, 7, {'As3': SkillProgress(3)},
                                 {'Ax': ModuleProgress(3, True), 'Ay': ModuleProgress(1, False)})
        self.box = OperatorBox('fixture', '2026-09-24', 'synthetic', {'A': self.operator})

    def match(self, opers=None, groups=None):
        return match_candidate(self.box, candidate(opers, groups), self.catalog)

    def test_exact_and_effective_mastery(self):
        result = self.match([spec(elite=2, level=90, skill_level=10, module=1, module_level=3, potential=6)])
        self.assertEqual(result.status, 'exact')
        self.assertEqual(effective_skill_level(self.operator, 'As3'), 10)
        self.assertFalse(result.to_dict()['support_needed'])
        self.assertNotIn('account_id', str(result.to_dict()))

    def test_each_insufficient_requirement_is_one_replacement(self):
        for change, req, reason in [
            ({'elite': 1}, {'elite': 2}, 'elite'),
            ({'level': 59}, {'level': 60}, 'level'),
            ({'skills': {'As3': SkillProgress(2)}}, {'skill_level': 10}, 'skill_level'),
            ({'modules': {'Ax': ModuleProgress(2, True)}}, {'module': 1, 'module_level': 3}, 'module_level'),
            ({'potential': 1}, {'potential': 2}, 'potential'),
        ]:
            with self.subTest(reason=reason):
                self.box = replace(self.box, operators={'A': replace(self.operator, **change)})
                result = self.match([spec(**req)])
                self.assertEqual(result.status, 'support_one')
                self.assertIn(reason, next(iter(result.slots.values()))[0].unsatisfied)
                self.assertEqual(result.support_operator, 'A')

    def test_missing_one_and_multiple(self):
        self.assertEqual(self.match([spec('B')]).status, 'support_one')
        self.assertEqual(self.match([spec('B'), spec('C')]).status, 'incompatible')
        self.assertEqual(self.match([spec('A'), spec('B')]).status, 'support_one')

    def test_group_alternative(self):
        self.assertEqual(self.match(groups=[group('B', 'A')]).status, 'exact')

    def test_global_group_assignment(self):
        self.box.operators['B'] = replace(self.operator, id='B')
        result = self.match(groups=[group('A', 'B'), group('A')])
        self.assertEqual(result.status, 'exact')
        self.assertEqual(set(result.assignments.values()), {'A', 'B'})
        reversed_result = self.match(groups=[group('B', 'A'), group('A')])
        self.assertEqual(result.assignments, reversed_result.assignments)

    def test_fixed_operator_reserves_group_identity(self):
        result = self.match([spec('A')], [group('A', 'B')])
        self.assertEqual(result.status, 'support_one')
        self.assertEqual(result.support_operator, 'B')
        self.assertEqual(list(result.assignments.values()), ['A'])

    def test_duplicate_identity_cannot_be_support(self):
        for opers, groups in [([spec('A'), spec('A')], []),
                              ([spec('A')], [group('A')]),
                              ([], [group('B'), group('B')])]:
            self.assertEqual(self.match(opers, groups).status, 'incompatible')

    def test_unknown_fields_never_exact_or_support(self):
        for req in ({'future': 0}, {'module': 'X'}, {'elite': True}, {'level': -1},
                    {'skill_level': 11}, {'elite': None}, {'module_level': 3}):
            with self.subTest(req=req):
                self.assertEqual(self.match([spec(**req)]).status, 'unknown')
                self.assertEqual(self.match([spec('B', **req)]).status, 'unknown')

    def test_unknown_provider_data(self):
        for change, req in [({'elite': None}, {'elite': 2}),
                            ({'level': None}, {'level': 60}),
                            ({'main_skill_level': None}, {'skill_level': 7}),
                            ({'skills': None}, {'skill_level': 10}),
                            ({'skills': {}}, {'skill_level': 10}),
                            ({'skills': {'As3': SkillProgress(None)}}, {'skill_level': 10}),
                            ({'modules': None}, {'module': 1}),
                            ({'modules': {}}, {'module': 1}),
                            ({'modules': {'Ax': ModuleProgress(3, None)}}, {'module': 1}),
                            ({'modules': {'Ax': ModuleProgress(None, True)}}, {'module': 1})]:
            with self.subTest(change=change):
                self.box = replace(self.box, operators={'A': replace(self.operator, **change)})
                self.assertEqual(self.match([spec(**req)]).status, 'unknown')

    def test_zero_and_omitted_requirements(self):
        self.box = replace(self.box, operators={'A': replace(self.operator, skills=None, modules=None)})
        self.assertEqual(self.match([spec(skill=None, elite=0, level=0, skill_level=0,
                                          module=0, module_level=0, potential=0)]).status, 'exact')
        self.assertEqual(self.match([spec(skill=None, skill_level=7)]).status, 'exact')
        self.assertEqual(self.match([spec(skill=0, skill_level=10)]).status, 'unknown')

    def test_skill_unlock_and_lower_base(self):
        self.box = replace(self.box, operators={'A': replace(self.operator, elite=1, main_skill_level=6)})
        self.assertEqual(self.match([spec()]).status, 'support_one')
        self.assertEqual(effective_skill_level(self.box.operators['A'], 'As3'), 6)
        self.assertEqual(self.match([spec(skill=1, skill_level=7)]).status, 'support_one')

    def test_module_selection_is_not_any_module(self):
        self.assertEqual(self.match([spec(module=2)]).status, 'support_one')
        self.assertEqual(self.match([spec(module=3)]).status, 'unknown')

    def test_canonical_skill_not_dictionary_position(self):
        self.box = replace(self.box, operators={'A': replace(self.operator, skills={
            'As3': SkillProgress(3), 'As1': SkillProgress(0), 'As2': SkillProgress(1)})})
        self.assertEqual(self.match([spec(skill=1, skill_level=10)]).status, 'support_one')
        self.assertEqual(self.match([spec(skill=3, skill_level=10)]).status, 'exact')

    def test_unknown_mapping_and_ambiguous_name(self):
        self.assertEqual(self.match([spec('unmapped')]).status, 'unknown')
        self.catalog = OperatorCatalog([OperatorIdentity('A', 'A'), OperatorIdentity('B', 'A')])
        self.assertEqual(self.match([spec('A')]).status, 'unknown')

    def test_partial_battle_catalog(self):
        self.catalog = OperatorCatalog.from_battle_data({'chars': {'A': {'name': 'A'}}})
        self.assertEqual(self.match([spec()]).status, 'unknown')
        self.assertEqual(self.match([spec(skill=0)]).status, 'exact')
        self.catalog = OperatorCatalog.from_battle_data({'chars': {'A': {'name': 'A'}}},
                                                       skills={'A': {3: 'As3'}}, modules={'A': {1: 'Ax'}})
        self.assertEqual(self.match([spec(skill_level=10, module=1)]).status, 'exact')

    def test_catalog_validation(self):
        for rows in [[OperatorIdentity('A', 'A'), OperatorIdentity('A', 'B')],
                     [OperatorIdentity('A', 'A', {0: 's'})],
                     [OperatorIdentity('A', 'A', {1: 's', 2: 's'})]]:
            with self.assertRaises(ValueError):
                OperatorCatalog(rows)

    def test_unknown_plus_one_or_two_missing(self):
        self.assertEqual(self.match([spec(level=None), spec('B')]).status, 'unknown')
        self.assertEqual(self.match([spec(level=None), spec('B'), spec('C')]).status, 'incompatible')

    def test_irrelevant_unknown_group_member(self):
        choices = dict(name='g', operators=[spec('A'), spec('B', future=1)])
        self.assertEqual(self.match(groups=[choices]).status, 'exact')

    def test_ranking_and_id_tie_breaker(self):
        candidates = [candidate([spec('B'), spec('C')], id=1, hot_score=100),
                      candidate([spec(future=1)], id=2, hot_score=100),
                      candidate([spec('B')], id=3, hot_score=100),
                      candidate([spec()], id=4, hot_score=1),
                      candidate([spec()], id=5, hot_score=2),
                      candidate([spec()], id=6, hot_score=2, rating_level=1),
                      candidate([spec()], id=7, hot_score=2, rating_level=1)]
        for order in (candidates, list(reversed(candidates))):
            self.assertEqual([r.copilot_id for r in rank_candidates(self.box, order, self.catalog)],
                             [6, 7, 5, 4, 3, 2, 1])
        for value in (None, float('nan'), float('inf'), True, '9'):
            result = rank_candidates(self.box, [candidate(id=2, hot_score=value), candidate(id=1)], self.catalog)
            self.assertEqual([r.copilot_id for r in result], [1, 2])
        with self.assertRaises(ValueError):
            rank_candidates(self.box, [candidate(), candidate()], self.catalog)

    def test_all_small_group_graphs_against_exhaustive_oracle(self):
        self.box.operators.update({name: replace(self.operator, id=name) for name in 'BC'})
        choices = [('A',), ('B',), ('C',), ('A', 'B'), ('B', 'C'), ('A', 'B', 'C')]
        for graph in itertools.product(choices, repeat=3):
            expected = any(len(set(pick)) == 3 for pick in itertools.product(*graph))
            result = self.match(groups=[group(*names) for names in graph])
            self.assertEqual(result.status, 'exact' if expected else 'incompatible', graph)

    def test_group_support_against_exhaustive_oracle(self):
        self.box.operators['B'] = replace(self.operator, id='B')
        choices = [('A',), ('B',), ('C',), ('A', 'B'), ('B', 'C'), ('C', 'D')]
        for graph in itertools.product(choices, repeat=3):
            missing = [sum(name not in self.box.operators for name in pick)
                       for pick in itertools.product(*graph) if len(set(pick)) == 3]
            best = min(missing, default=3)
            expected = {0: 'exact', 1: 'support_one'}.get(best, 'incompatible')
            result = self.match(groups=[group(*names) for names in graph])
            self.assertEqual(result.status, expected, graph)
            if result.support_needed:
                self.assertNotIn(result.support_operator, result.assignments.values())


if __name__ == '__main__':
    unittest.main()
