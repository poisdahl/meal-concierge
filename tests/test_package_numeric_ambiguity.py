"""Do not turn ambiguous display separators into exact shopping capacity."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from product_observations import parse_package, parse_variable_package


class PackageNumericTests(unittest.TestCase):
    def test_ambiguous_thousands_or_decimal_display_is_not_exact(self):
        for provider in (None, 'oda', 'mathem', 'meny'):
            for amount in ('1.000', '1,000', '12.500', '12,500', '999.999'):
                for label in (f'{amount} g', f'2 x {amount} g'):
                    with self.subTest(provider=provider, label=label):
                        self.assertIsNone(parse_package(label, provider=provider))

    def test_approximate_weight_does_not_resolve_an_ambiguous_number(self):
        for provider in ('oda', 'mathem'):
            for value in ('ca. 1.000 g', 'minst 1,000 g'):
                self.assertIsNone(parse_variable_package(value, provider=provider))

    def test_unambiguous_decimal_and_integer_amounts_are_unchanged(self):
        for label, amount in [('1000 g', 1000), ('1,5 kg', 1500), ('0.125 kg', 125), ('0,125 kg', 125)]:
            self.assertEqual(parse_package(label)['quantity'], {'numerator': amount, 'denominator': 1})


if __name__ == '__main__':
    unittest.main()
