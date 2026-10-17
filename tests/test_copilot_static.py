import unittest

from maa_planner.copilot_static import build_catalog
from maa_planner.prts import PrtsError


class ModuleIdentityTests(unittest.TestCase):
    def catalog(self, modules):
        return build_catalog({'char_a': {'name': 'A', 'skills': [{'skillId': 'skill'}]}},
            {'charEquip': {'char_a': list(modules)}, 'equipDict': modules},
            {'chars': {'char_a': {'name': 'A'}}})

    def test_native_type_mapping_is_independent_of_display_and_source_array_order(self):
        catalog = self.catalog({'delta': {'typeName2': 'D', 'charEquipOrder': 3},
            'base': {'typeName2': None, 'charEquipOrder': 0},
            'gamma': {'typeName2': 'Y', 'charEquipOrder': 2},
            'alpha': {'typeName2': 'A', 'charEquipOrder': 4},
            'chi': {'typeName2': 'X', 'charEquipOrder': 1},
            'beta': {'typeName2': 'B', 'charEquipOrder': 5}})
        self.assertEqual(catalog.resolve('A').modules,
                         {1: 'chi', 2: 'gamma', 3: 'alpha', 4: 'delta', 5: 'beta'})

    def test_unknown_future_type_is_not_mapped_from_order_or_treated_as_original(self):
        catalog = self.catalog({'future': {'typeName2': 'FUTURE', 'charEquipOrder': 1},
                                'known': {'typeName2': 'Y', 'charEquipOrder': 2}})
        self.assertEqual(catalog.resolve('A').modules, {2: 'known'})
        self.assertEqual(catalog.resolve('A').skills, {1: 'skill'})

    def test_conflicting_native_types_are_rejected(self):
        with self.assertRaisesRegex(PrtsError, 'Ambiguous native module'):
            self.catalog({'one': {'typeName2': 'D', 'charEquipOrder': 3},
                          'two': {'typeName2': 'D', 'charEquipOrder': 4}})
