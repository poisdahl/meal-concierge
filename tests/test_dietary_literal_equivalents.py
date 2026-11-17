"""Explicit food-name equivalents add conflicts without guessing word stems."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dietary_assessment import assess, covered


class LiteralFoodNameTests(unittest.TestCase):
    def finding(self, term, food, *, field='ingredients', recipe=False, name='Synthetic product', kind='allergy'):
        profile = {'diet': {'rules': [{'kind': kind, 'term': term}], 'uncertainty_permissions': []}}
        item = {'name': name, 'product_ref': 1, 'ingredients': [{'item': food}]} if recipe else {
            'name': food if field == 'product_name' else name, 'product_ref': 1,
            'dietary_evidence': {} if field == 'product_name' else {field: food}}
        return assess(profile, item, recipe=recipe)[0]

    def test_demonstrated_compounds_and_inflections_are_literal_positive_evidence_in_every_field(self):
        examples = [('melk', 'helmelk'), ('milk', 'kremfløte'), ('melk', 'lettmelk'),
                    ('nøtter', 'valnøtter'), ('valnøtt', 'valnøtter'),
                    ('egg', 'eggs'), ('egg', 'eggeplommer'), ('egg', 'eggehviter'),
                    ('reke', 'reker'), ('shrimp', 'reker')]
        for term, food in examples:
            for field in ('ingredients', 'allergens', 'may_contain', 'product_name'):
                with self.subTest(term=term, food=food, field=field):
                    finding = self.finding(term, food, field=field)
                    self.assertEqual(finding['condition'], 'conflict')
                    self.assertTrue(finding['blocked'])
            self.assertEqual(self.finding(term, food, recipe=True)['condition'], 'conflict')
            self.assertEqual(self.finding(term, food, recipe=True, kind='never_buy')['condition'], 'conflict')

    def test_negative_exception_corpus_stays_unknown_without_inventing_suitability(self):
        for term, food in [('egg', 'kyllinglegg'), ('egg', 'eggplant'), ('egg', 'eggfri eggeplommer'),
                           ('hvete', 'bokhvete'), ('nøtt', 'muskatnøtt'), ('nøtter', 'muskatnøtter'),
                           ('nøtter', 'nøttefri valnøtter'), ('valnøtt', 'hasselnøtter'),
                           ('melk', 'kokosmelk'), ('melk', 'soya melk'), ('melk', 'melkefri kremfløte'),
                           ('melk', 'melkefri melkesjokolade'), ('milk', 'milk-free helmelk'),
                           ('milk', 'without helmelk'), ('reke', 'uten reker')]:
            for field, recipe in (('ingredients', False), ('product_name', False), ('ingredients', True)):
                with self.subTest(term=term, food=food, recipe=recipe, field=field):
                    finding = self.finding(term, food, recipe=recipe, field=field)
                    self.assertEqual(finding['condition'], 'unknown')
                    self.assertFalse(finding['blocked'])

    def test_lactose_free_is_not_milk_free_and_declared_allergens_override_a_title_claim(self):
        self.assertEqual(self.finding('melk', 'laktosefri helmelk')['condition'], 'conflict')
        self.assertEqual(self.finding('melk', 'helmelk', field='allergens', name='Melkefri kake')['condition'], 'conflict')
        self.assertEqual(self.finding('egg', 'eggeplommer', field='may_contain', name='Eggfri kake')['condition'], 'conflict')
        self.assertEqual(self.finding('melk', 'melkefri kake med helmelk')['condition'], 'conflict')

    def test_new_known_conflicts_do_not_accept_an_old_unknown_permission(self):
        finding = self.finding('melk', 'helmelk')
        profile = {'diet': {'uncertainty_permissions': [{'kind': 'allergy', 'term': 'melk',
            'product_ref': 1, 'condition': 'unknown', 'accepted': True, 'notify': True}]}}
        self.assertFalse(covered(profile, finding))


if __name__ == '__main__':
    unittest.main()
