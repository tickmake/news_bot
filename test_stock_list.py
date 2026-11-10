"""stock_list.csv stays complete and consistent."""

import re
import unittest

from newsbot import stock_list


class StockListTests(unittest.TestCase):
    def setUp(self):
        self.rows = stock_list.load()

    def test_every_row_has_every_column_filled(self):
        self.assertGreaterEqual(len(self.rows), 90)
        for row in self.rows:
            self.assertEqual(list(row), stock_list.COLUMNS)
            self.assertTrue(all(value.strip() for value in row.values()), row)

    def test_tickers_and_isins_are_unique_and_well_formed(self):
        self.assertEqual(len({row["ticker"] for row in self.rows}), len(self.rows))
        self.assertEqual(len({row["isin"] for row in self.rows}), len(self.rows))
        for row in self.rows:
            self.assertRegex(row["isin"], r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
            self.assertIn(row["region"], {"Oslo", "EU", "US"})
            self.assertIn(row["ask_allowed"], {"yes", "no"})

    def test_us_stocks_are_never_marked_ask_allowed(self):
        for row in self.rows:
            if row["isin"].startswith("US"):
                self.assertEqual(row["ask_allowed"], "no", row["ticker"])

    def test_isin_check_digit(self):
        # Luhn over the ISIN with letters as numbers (A=10 ... Z=35).
        for row in self.rows:
            digits = "".join(str(int(ch, 36)) for ch in row["isin"])
            total = 0
            for position, digit in enumerate(reversed(digits)):
                value = int(digit) * (2 if position % 2 else 1)
                total += value // 10 + value % 10
            self.assertEqual(total % 10, 0, row["isin"])

    def test_tickers_helper(self):
        self.assertIn("EQNR.OL", stock_list.tickers())
        self.assertTrue(all(re.match(r"^[A-Z0-9.\-]+$", ticker) for ticker in stock_list.tickers()))


if __name__ == "__main__":
    unittest.main()
