"""真实公开报价固定样例 + 会影响信号正确性的解析/身份/时间边界。

普通测试不访问网络。样例于 2026-11-08 抓取，是休市历史报价。
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

from contracts import normalize_contract, supported_products, EASTMONEY_MARKETS
from market_data import (parse_sina, parse_eastmoney, parse_history,
                         collect_quotes, is_fresh, fetch_eastmoney, QuoteError)


FIXTURES = Path(__file__).parent / "fixtures"
SHANGHAI = timezone(timedelta(hours=8))


def fixture_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ContractTests(unittest.TestCase):
    def test_meal_specific_month_and_unit(self):
        c = normalize_contract(" M2709 ")
        self.assertEqual(c["symbol"], "m2709")
        self.assertEqual((c["year"], c["month"]), (2027, 9))
        self.assertEqual((c["exchange"], c["multiplier"], c["tick_size"]), ("DCE", 10, 1))
        self.assertEqual(c["sina_symbol"], "M2709")
        self.assertEqual(c["eastmoney_secid"], "114.m2709")

    def test_czce_uses_different_codes_for_two_providers(self):
        for code in ("SA701", "sa2701"):
            c = normalize_contract(code, as_of="2026-11-08")
            self.assertEqual(c["symbol"], "sa2701")
            self.assertEqual(c["sina_symbol"], "SA2701")
            self.assertEqual(c["eastmoney_secid"], "115.SA701")
        self.assertEqual(normalize_contract("SA001", as_of="2029-11-08")["year"], 2030)

    def test_cffex_market_id_matches_official_directory(self):
        directory = fixture_json("eastmoney_markets.json")
        markets = {row["mktshort"]: int(row["mktid"]) for row in directory}
        self.assertEqual(EASTMONEY_MARKETS, markets)
        self.assertEqual(normalize_contract("IF2611")["eastmoney_secid"], "220.IF2611")

    def test_exchange_identifiers_are_validated(self):
        self.assertEqual(normalize_contract("DCE.m2709")["symbol"], "m2709")
        self.assertEqual(normalize_contract("m2709.DCE")["symbol"], "m2709")
        with self.assertRaises(ValueError):
            normalize_contract("m2709", exchange="SHFE")
        with self.assertRaises(ValueError):
            normalize_contract("m2709.DCE", exchange="INE")

    def test_continuous_invalid_and_unrecognized_codes_fail_closed(self):
        for code in ("M0", "M888", "M9999", "m2700", "m2713", "m709", "xyz2709",
                     "m2709;curl", "m2709/../../etc", "", "m2709.DCE.INE"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                normalize_contract(code)

    def test_all_metadata_maps_to_consistent_exchange_and_specific_month(self):
        for row in supported_products():
            with self.subTest(product=row["product"]):
                c = normalize_contract(row["product"] + "2701")
                self.assertGreater(c["multiplier"], 0)
                self.assertGreater(c["tick_size"], 0)
                self.assertEqual(c["exchange"], row["exchange"])


class QuoteParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sina = (FIXTURES / "sina_quotes.txt").read_text(encoding="utf-8")

    def test_real_meal_quotes_agree_without_replacing_market_timestamp(self):
        contract = normalize_contract("m2709")
        received = "2026-11-08T15:00:00+08:00"
        a = parse_sina(self.sina, contract, received_at=received)
        b = parse_eastmoney(fixture_json("eastmoney_m2709.json"), contract, received_at=received)
        for row in (a, b):
            self.assertTrue(row["ok"])
            self.assertEqual(row["price"], 2904)
            self.assertEqual(row["quote_at"], "2026-11-06T23:00:00+08:00")
            self.assertEqual(row["received_at"], received)
            self.assertFalse(is_fresh(row, now=received))

    def test_financial_sina_layout_and_eastmoney_decimal(self):
        c = normalize_contract("IF2611")
        a = parse_sina(self.sina, c)
        b = parse_eastmoney(fixture_json("eastmoney_if2611.json"), c)
        self.assertEqual(a["price"], 4039.6)
        self.assertEqual(b["price"], 4039.6)
        self.assertEqual(a["quote_at"], "2026-11-06T15:00:00+08:00")
        self.assertEqual(a["volume"], 26990)
        self.assertEqual(a["open_interest"], 50123)

    def test_fractional_gold_quote_and_overnight_natural_date(self):
        c = normalize_contract("au2612")
        for row in (parse_sina(self.sina, c),
                    parse_eastmoney(fixture_json("eastmoney_au2612.json"), c)):
            self.assertEqual(row["price"], 874.58)
            self.assertEqual(row["quote_at"], "2026-11-07T02:30:00+08:00")

    def test_czce_ine_and_gfex_samples(self):
        for symbol, filename, price in (("sa2701", "eastmoney_sa701.json", 992),
                                        ("sc2612", "eastmoney_sc2612.json", 639.1),
                                        ("lc2701", "eastmoney_lc2701.json", 92860)):
            with self.subTest(symbol=symbol):
                c = normalize_contract(symbol)
                self.assertEqual(parse_sina(self.sina, c)["price"], price)
                self.assertEqual(parse_eastmoney(fixture_json(filename), c)["price"], price)

    def test_czce_old_decade_cannot_alias_current_contract(self):
        with self.assertRaises(QuoteError):
            parse_eastmoney(fixture_json("eastmoney_sa701.json"), normalize_contract("sa1701"))
        with self.assertRaises(QuoteError):
            parse_eastmoney(fixture_json("eastmoney_sa701.json"), normalize_contract("sa3701"))

    def test_sina_empty_wrong_duplicate_or_bad_timestamp_never_valid(self):
        c = normalize_contract("m2709")
        examples = ['var hq_str_nf_M2709="";',
                    'var hq_str_nf_M2701="";',
                    self.sina + self.sina,
                    self.sina.replace("2026-11-06", ""),
                    self.sina.replace("230000", "299999")]
        for text in examples:
            with self.subTest(text=text[:80]), self.assertRaises(QuoteError):
                parse_sina(text, c)

    def test_wrong_eastmoney_identity_scaling_and_missing_date_never_valid(self):
        c = normalize_contract("m2709")
        for key, value in (("f57", "m2701"), ("f59", None), ("f59", "-"),
                           ("f86", 0), ("f86", None), ("f86", 1793977200000),
                           ("f43", 0), ("f43", -1), ("f43", "NaN"),
                           ("f43", 2904.5), ("f43", True)):
            payload = fixture_json("eastmoney_m2709.json")
            payload["data"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(QuoteError):
                parse_eastmoney(payload, c)
        with self.assertRaises(QuoteError):
            parse_eastmoney({"rc": 100, "data": None}, c)

    def test_freshness_rejects_stale_future_and_naive_timestamps(self):
        q = parse_eastmoney(fixture_json("eastmoney_m2709.json"), normalize_contract("m2709"))
        self.assertTrue(is_fresh(q, max_age_seconds=60, now="2026-11-06T23:00:30+08:00"))
        self.assertFalse(is_fresh(q, max_age_seconds=60, now="2026-11-06T23:02:00+08:00"))
        self.assertFalse(is_fresh(q, now="2026-11-06T22:59:00+08:00"))
        q["quote_at"] = "2026-11-06T23:00:00"
        self.assertFalse(is_fresh(q, now="2026-11-06T23:00:10+08:00"))

    def test_eastmoney_fallback_uses_independent_endpoint(self):
        contract = normalize_contract("m2709")
        payload = (FIXTURES / "eastmoney_m2709.json").read_bytes()
        called = []
        def download(url, **kwargs):
            called.append((url, kwargs["timeout"]))
            if "push2.eastmoney.com" in url:
                raise urllib.error.URLError("SSL handshake timed out")
            return payload
        with patch("market_sources._http_bytes", side_effect=download):
            quote = fetch_eastmoney(contract, timeout=3)
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["endpoint"], "push2delay.eastmoney.com")
        self.assertEqual(len(called), 2)
        self.assertLessEqual(called[0][1], 1.7)
        self.assertLessEqual(called[1][1], 3)

    def test_eastmoney_fallback_preserves_failure_not_fake_quote(self):
        contract = normalize_contract("m2709")
        with patch("market_sources._http_bytes", side_effect=TimeoutError("SSL timeout")) as mocked:
            with self.assertRaisesRegex(QuoteError, "备用接口也不可用"):
                fetch_eastmoney(contract, timeout=2)
        self.assertEqual(mocked.call_count, 2)

    def test_one_failed_source_cannot_erase_other_source(self):
        c = normalize_contract("m2709")
        good = parse_eastmoney(fixture_json("eastmoney_m2709.json"), c)
        from market_sources import SOURCE_FETCHERS
        with patch.dict(SOURCE_FETCHERS, {"sina": lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("offline")),
                                          "emfutures": lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("temporarily unavailable")),
                                          "eastmoney": lambda *_a, **_k: good}):
            result = collect_quotes(c, timeout=1)
        self.assertEqual([x["source"] for x in result], ["sina", "emfutures", "eastmoney"])
        self.assertFalse(result[0]["ok"])
        self.assertIsNone(result[0]["price"])
        self.assertIn("offline", result[0]["error"])
        self.assertFalse(result[1]["ok"])  # 东财专线失败，但其优先级仍为第二
        self.assertTrue(result[2]["ok"])
        self.assertEqual(result[2]["price"], 2904)


class HistoricalDataTests(unittest.TestCase):
    def test_history_is_filtered_specific_contract_daily_close(self):
        payload = fixture_json("eastmoney_m2709_history.json")
        rows = parse_history(payload, normalize_contract("m2709"), "2026-09-15", "2026-09-17")
        self.assertEqual([r["time"] for r in rows], ["2026-09-15", "2026-09-16", "2026-09-17"])
        self.assertEqual([r["price"] for r in rows], [3098, 3157, 3171])
        self.assertEqual(rows[0]["open"], 3102)

    def test_history_wrong_contract_or_exchange_and_reversed_dates_rejected(self):
        payload = fixture_json("eastmoney_m2709_history.json")
        c = normalize_contract("m2709")
        for key, value in (("code", "m0"), ("market", 113)):
            changed = deepcopy(payload)
            changed["data"][key] = value
            with self.subTest(key=key), self.assertRaises(QuoteError):
                parse_history(changed, c, "20260101", "20261231")
        with self.assertRaises(ValueError):
            parse_history(payload, c, "2026-12-31", "2026-01-01")
        with self.assertRaises(QuoteError):
            parse_history({"rc": 0, "data": None}, c, "20260101", "20261231")


if __name__ == "__main__":
    unittest.main()
