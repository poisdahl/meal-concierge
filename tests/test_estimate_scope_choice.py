"""Estimate-mode candidate choice stays explicit about unobserved payable costs."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from product_planner import build_product_plan, menu_requirements, _estimated_product_scope, _practical_packages
from service import Application
from test_meal_concierge_products import menu, observation, product


def regular_price(amount):
    return {'package_count': 1, 'price_kind': 'exact', 'merchandise_ore': amount,
            'mandatory_deposit_ore': None, 'total_payable_ore': None,
            'offer_kind': 'regular', 'eligibility': 'confirmed'}


class EstimateScopeTests(unittest.TestCase):
    def setUp(self):
        self.menu = menu({'item': 'oat drink', 'quantity': 450, 'unit': 'ml'})
        self.need = menu_requirements(self.menu)[0][0]
        self.small = product('10', 'Oat drink small', 500, 'ml', [regular_price(2000)])
        self.bulk = product('20', 'Oat drink six pack', 3000, 'ml', [regular_price(9000)])

    def plan(self, refs=('10', '20'), products=None, **kwargs):
        return build_product_plan(provider='oda', binding={}, menu=self.menu, price_mode='estimate',
            observations={self.need['requirement_id']: observation('oat drink', products or [self.small, self.bulk])},
            candidate_approvals=[{'requirement_id': self.need['requirement_id'], 'candidate_refs': list(refs)}], **kwargs)

    def test_multiple_approved_candidates_choose_lowest_covering_merchandise_cost(self):
        plan = self.plan()
        self.assertEqual(plan['status'], 'prepared')
        selection = plan['requirements'][0]['selection']
        self.assertEqual(selection['products'][0]['product_ref'], '10')
        self.assertEqual(plan['totals']['merchandise_ore'], 2000)
        self.assertIsNone(plan['totals']['total_payable_ore'])
        self.assertIsNone(plan['comparison_claim'])
        self.assertEqual(plan['requirements'][0]['eligible_candidate_count'], 2)

    def test_bulk_pack_surplus_is_visible_without_increasing_the_need(self):
        plan = self.plan(('20',))
        selection = plan['requirements'][0]['selection']
        self.assertEqual(selection['quantity_warnings'][0]['reason'], 'oversized_package')
        self.assertEqual(selection['required'], {'numerator': 450, 'denominator': 1})
        self.assertEqual(selection['surplus_quantity'], {'numerator': 2550, 'denominator': 1})

    def test_unknown_scope_member_is_not_silently_excluded_from_comparison(self):
        unknown = deepcopy(self.bulk)
        unknown['purchase_options'][0]['eligibility'] = 'unknown'
        self.assertEqual(self.plan(products=[self.small, unknown])['status'], 'needs_input')

    def test_dietary_rank_remains_ahead_of_price(self):
        self.small['dietary_findings'] = [{'condition': 'preference_deviation'}]
        result = _estimated_product_scope(self.need, {'products': [self.small, self.bulk]},
                                         {'candidate_refs': ['10', '20']})
        self.assertEqual(result['products'][0]['product_ref'], '20')

    def test_unknown_variable_surplus_is_not_ranked_as_zero_waste(self):
        variable = deepcopy(self.bulk)
        variable['package']['quantity_kind'] = 'expected'
        variable['purchase_options'] = [regular_price(2000)]
        result = self.plan(products=[self.small, variable])
        self.assertEqual(result['requirements'][0]['selection']['products'][0]['product_ref'], '10')

    def test_surplus_warning_survives_the_saved_agent_review_snapshot(self):
        plan = self.plan(('20',))
        shown = Application._product_review_snapshot(plan)
        self.assertEqual(shown['requirements'][0]['selection']['quantity_warnings'],
                         plan['requirements'][0]['selection']['quantity_warnings'])

    def test_explicit_practical_extra_packages_have_a_quantity_warning(self):
        choice = {'candidate_refs': ['10'], 'package_count': 2,
                  'quantity_basis': 'Two observed packages chosen for this shop.'}
        selected = _practical_packages(self.need, {'products': [self.small]}, choice, 'estimate')
        self.assertEqual(selected['quantity_warnings'][0]['reason'], 'extra_packages')
        self.assertEqual(selected['quantity_warnings'][0]['minimum_covering_packages'], 1)


if __name__ == '__main__':
    unittest.main()
