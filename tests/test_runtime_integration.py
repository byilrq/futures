"""事务、行情边界与回滚业务测试；全部使用人工可控报价，绝不推送外网。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from config import SHANGHAI, SYSTEM_DEFAULTS, PUSH_DEFAULTS, stamp
from contracts import normalize_contract
from storage import Store, Conflict
from service import add_symbol, save_config, manual_fill, rollback
from runtime import process_quotes


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="futures-test-")
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name).initialize()
        self.symbol_id = add_symbol(self.store, normalize_contract("m2709"))
        self.at = datetime(2026, 11, 9, 10, 0, tzinfo=SHANGHAI)
        with self.store.tx() as con:
            system = {**SYSTEM_DEFAULTS, "strategy_enabled": True}
            self.store.set_setting(con, "system", system)
            # Enable only the local outbox state machine. No sender is called.
            self.store.set_setting(con, "push", {**PUSH_DEFAULTS, "PUSH_ENABLED": "yes"})
        save_config(self.store, self.symbol_id, {
            "enabled": True, "lower_price": "3000", "upper_price": "3500",
            "down_step": "100", "up_step": "100", "signal_lots": 1,
            "source": "inherit", "rearm_mode": "return",
        }, self.symbol()["revision"])

    def symbol(self):
        with self.store.read() as con:
            return self.store.symbol(con, self.symbol_id)

    def count(self, table, condition="1=1"):
        with self.store.read() as con:
            return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {condition}").fetchone()[0]

    def quotes(self, price, minute=0, source="sina", symbol="m2709", at=None):
        when = at or self.at + timedelta(minutes=minute)
        return [{"source": source, "label": "新浪期货" if source == "sina" else "东方财富",
                 "symbol": symbol, "price": price, "quote_at": stamp(when),
                 "received_at": stamp(when), "ok": True, "error": "", "raw_code": symbol}]

    def process(self, price, minute=0, source="sina", at=None):
        when = at or self.at + timedelta(minutes=minute)
        return process_quotes(self.store, self.symbol_id,
                              self.quotes(price, source=source, at=when), at=when)

    def fill(self, side="open_long", lots="1", price="3100", notify=True, request_id=None):
        return manual_fill(self.store, self.symbol_id, {
            "request_id": request_id or uuid.uuid4().hex,
            "revision": self.symbol()["revision"], "side": side, "lots": lots,
            "price": price, "fee": "0", "notify": notify,
        })

    def test_first_sample_only_baselines_and_real_crossing_records_signal_without_position(self):
        self.assertEqual(self.process("3100")["signals"], [])
        result = self.process("2999", 1)
        self.assertEqual([s["level"] for s in result["signals"]], ["3000"])
        symbol = self.symbol()
        self.assertEqual(symbol["state"]["portfolio"]["long_lots"], 0)
        self.assertEqual(symbol["state"]["portfolio"]["short_lots"], 0)
        self.assertEqual(self.count("events", "kind='signal'"), 1)
        self.assertEqual(self.count("outbox", "kind='signal'"), 1)

    def test_identical_quote_is_idempotent_even_under_concurrent_calls(self):
        self.process("3100")
        at = self.at + timedelta(minutes=1)
        quotes = self.quotes("2999", at=at)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: process_quotes(self.store, self.symbol_id, quotes, at=at), range(8)))
        self.assertEqual(sum(len(x["signals"]) for x in results), 1)
        self.assertEqual(self.count("events", "kind='signal'"), 1)
        self.assertEqual(self.count("price_history"), 2)
        self.assertEqual(self.count("outbox", "kind='signal'"), 1)

    def test_multi_level_gap_is_one_atomic_push_envelope_with_all_signal_records(self):
        self.process("3100")
        result = self.process("2799", 1)
        self.assertEqual([x["level"] for x in result["signals"]], ["3000", "2900", "2800"])
        self.assertEqual(self.count("events", "kind='signal'"), 3)
        self.assertEqual(self.count("outbox", "kind='signal'"), 1)
        self.assertEqual(self.symbol()["state"]["strategy"]["next_buy"], "2700")
        with self.store.read() as con:
            ids = {r[0] for r in con.execute("SELECT snapshot_id FROM events WHERE kind='signal'")}
            self.assertEqual(ids, {result["snapshot_id"]})

    def test_source_switch_baselines_instead_of_crossing_between_two_vendors(self):
        self.process("3100")
        result = self.process("2899", 1, source="eastmoney")
        self.assertEqual(result["signals"], [])
        self.assertEqual(self.symbol()["state"]["strategy"]["next_buy"], "2800")
        later = self.process("2799", 2, source="eastmoney")
        self.assertEqual([x["level"] for x in later["signals"]], ["2800"])

    def test_fixed_source_is_priority_but_fails_over_when_offline(self):
        with self.store.tx() as con:
            system = self.store.setting(con, "system")
            system["quote_source"] = "sina"
            self.store.set_setting(con, "system", system)
        result = self.process("3100", source="eastmoney")
        self.assertEqual(result["signals"], [])
        # User preference is honoured until it fails; then fallback to the
        # next healthy source is persisted, rather than stopping altogether.
        self.assertNotEqual(self.symbol()["last_quote_epoch"], 0)
        self.assertEqual(self.count("price_history"), 1)
        self.assertEqual(self.symbol()["state"]["source_route"]["active"], "eastmoney")

    def test_simultaneous_source_disagreement_does_not_mutate_ladder(self):
        first = self.symbol()["state"]["strategy"]
        quotes = self.quotes("3100") + self.quotes("3200", source="eastmoney")
        result = process_quotes(self.store, self.symbol_id, quotes, at=self.at)
        self.assertEqual(result["signals"], [])
        self.assertEqual(self.symbol()["state"]["strategy"], first)
        self.assertEqual(self.symbol()["last_quote_epoch"], 0)
        self.assertEqual(self.count("price_history"), 0)

    def test_stale_future_wrong_symbol_and_out_of_order_quotes_never_generate_events(self):
        self.process("3100")
        previous = self.symbol()
        cases = [
            (self.quotes("2999", at=self.at - timedelta(minutes=10)), self.at),
            (self.quotes("2999", at=self.at + timedelta(minutes=10)), self.at),
            (self.quotes("2999", minute=1, symbol="m2701"), self.at + timedelta(minutes=1)),
            (self.quotes("2999", at=self.at - timedelta(seconds=1)), self.at),
        ]
        for quotes, now in cases:
            with self.subTest(quotes=quotes):
                result = process_quotes(self.store, self.symbol_id, quotes, at=now)
                self.assertEqual(result["signals"], [])
                self.assertEqual(self.symbol()["last_quote_epoch"], previous["last_quote_epoch"])
                self.assertEqual(self.symbol()["state"]["strategy"], previous["state"]["strategy"])
        self.assertEqual(self.count("events", "kind='signal'"), 0)

    def test_valid_closed_session_quote_only_updates_display_and_opening_gap_triggers(self):
        self.process("3100")
        before = self.symbol()
        history_count = self.count("price_history")
        self.assertEqual(self.process("2999", at=self.at.replace(hour=18))["signals"], [])
        after = self.symbol()
        self.assertEqual(after["state"]["strategy"], before["state"]["strategy"])
        self.assertEqual(after["last_quote_epoch"], before["last_quote_epoch"])
        self.assertEqual(self.count("price_history"), history_count)
        result = self.process("2899", at=self.at + timedelta(days=1))
        self.assertEqual([x["level"] for x in result["signals"]], ["3000", "2900"])

    def test_closed_duplicate_or_stale_quotes_keep_prior_session_baseline(self):
        close_at = self.at.replace(hour=14, minute=59)
        self.process("3100", at=close_at)
        before = self.symbol()
        quotes = self.quotes("3100", at=close_at)
        for when in (close_at + timedelta(minutes=2), close_at + timedelta(minutes=17)):
            result = process_quotes(self.store, self.symbol_id, quotes, at=when)
            self.assertEqual(result["signals"], [])
            self.assertEqual(self.symbol()["state"]["strategy"], before["state"]["strategy"])
            self.assertEqual(self.symbol()["last_quote_epoch"], before["last_quote_epoch"])
        result = self.process("2999", at=(self.at + timedelta(days=1)).replace(hour=9))
        self.assertEqual([x["level"] for x in result["signals"]], ["3000"])

    def test_master_pause_resume_baselines_and_does_not_chase_missed_crossings(self):
        self.process("3100")
        # Legacy per-symbol enabled flag can no longer pause a contract.
        save_config(self.store, self.symbol_id, {"enabled": False}, self.symbol()["revision"])
        self.assertTrue(self.symbol()["config"]["enabled"])
        # Only the system master switch blocks processing.
        with self.store.tx() as con:
            setting = self.store.setting(con, "system", SYSTEM_DEFAULTS)
            setting["strategy_enabled"] = False
            self.store.set_setting(con, "system", setting)
        self.assertEqual(self.process("2999", 1)["signals"], [])
        with self.store.tx() as con:
            setting = self.store.setting(con, "system", SYSTEM_DEFAULTS)
            setting["strategy_enabled"] = True
            self.store.set_setting(con, "system", setting)
        self.assertEqual(self.process("2899", 2)["signals"], [])
        result = self.process("2799", 3)
        self.assertEqual([x["level"] for x in result["signals"]], ["2800"])

    def test_failed_commit_rolls_back_price_watermark_ladder_events_and_outbox(self):
        self.process("3100")
        previous = self.symbol()
        counts = {t: self.count(t) for t in ("events", "snapshots", "price_history", "outbox")}
        with patch.object(self.store, "enqueue", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.process("2999", 1)
        self.assertEqual(self.symbol(), previous)
        self.assertEqual({t: self.count(t) for t in counts}, counts)
        self.assertEqual(len(self.process("2999", 1)["signals"]), 1)

    def test_manual_fill_is_atomic_and_duplicate_submit_cannot_double_position(self):
        request_id = uuid.uuid4().hex
        values = {"request_id": request_id, "revision": self.symbol()["revision"],
                  "side": "open_long", "lots": "2", "price": "3100", "fee": "4", "notify": True}
        first = manual_fill(self.store, self.symbol_id, values)
        second = manual_fill(self.store, self.symbol_id, values)
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.symbol()["state"]["portfolio"]["long_lots"], 2)
        self.assertEqual(self.count("events", "kind='manual'"), 1)
        self.assertEqual(self.count("outbox", "kind='manual'"), 1)
        before = self.symbol()
        counts = {t: self.count(t) for t in ("events", "snapshots", "requests", "outbox")}
        with patch.object(self.store, "enqueue", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.fill(lots="1")
        self.assertEqual(self.symbol(), before)
        self.assertEqual({t: self.count(t) for t in counts}, counts)

    def test_invalid_lots_excess_close_and_stale_revision_do_not_partially_write(self):
        original = self.symbol()
        count = self.count("events")
        for values in ({"lots": "1.5"}, {"lots": "0"}, {"side": "close_long", "lots": "1"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.fill(**values)
        self.assertEqual(self.symbol(), original)
        self.assertEqual(self.count("events"), count)
        self.fill()
        with self.assertRaises(Conflict):
            manual_fill(self.store, self.symbol_id, {"request_id": uuid.uuid4().hex,
                "revision": original["revision"], "side": "open_long", "lots": "1", "price": "3100"})
        self.assertEqual(self.symbol()["state"]["portfolio"]["long_lots"], 1)

    def test_rollback_cancels_pending_signals_keeps_watermark_and_never_replays_quote(self):
        self.process("3100")
        triggered = self.process("2999", 1)
        self.fill(lots="2")
        before = self.symbol()
        request_id = uuid.uuid4().hex
        result = rollback(self.store, self.symbol_id, triggered["snapshot_id"], before["revision"], request_id)
        after = self.symbol()
        self.assertEqual(after["last_quote_epoch"], before["last_quote_epoch"])
        self.assertEqual(after["state"]["market"], before["state"]["market"])
        self.assertEqual(after["state"]["portfolio"]["long_lots"], 0)
        self.assertTrue(after["state"]["strategy"]["needs_baseline"])
        self.assertEqual(self.count("outbox", "kind IN ('signal','manual') AND status='cancelled'"), 2)
        self.assertEqual(self.count("events", "kind IN ('signal','manual') AND reverted_by IS NOT NULL"), 2)
        again = rollback(self.store, self.symbol_id, triggered["snapshot_id"], before["revision"], request_id)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["event_id"], result["event_id"])
        self.assertEqual(self.process("2999", 1)["status"], "duplicate")
        self.assertEqual(self.process("2799", 2)["signals"], [])
        self.assertEqual([x["level"] for x in self.process("2699", 3)["signals"]], ["2700"])

    def test_rollback_cannot_restore_another_contract_snapshot(self):
        other_id = add_symbol(self.store, normalize_contract("rb2701"))
        with self.store.read() as con:
            other = self.store.symbol(con, other_id)
        manual_fill(self.store, other_id, {
            "request_id": uuid.uuid4().hex, "revision": other["revision"],
            "side": "open_long", "lots": "1", "price": "3100"})
        with self.store.read() as con:
            snapshot_id = con.execute("SELECT id FROM snapshots WHERE symbol_id=?", (other_id,)).fetchone()[0]
        before = self.symbol()
        with self.assertRaises(ValueError):
            rollback(self.store, self.symbol_id, snapshot_id, before["revision"], uuid.uuid4().hex)
        self.assertEqual(self.symbol(), before)

    def test_undoing_trade_before_event_does_not_resurrect_abandoned_branch(self):
        first = self.fill(lots="1")
        second = self.fill(lots="2")
        rollback(self.store, self.symbol_id, first["snapshot_id"], self.symbol()["revision"], uuid.uuid4().hex)
        self.assertEqual(self.symbol()["state"]["portfolio"]["long_lots"], 0)
        # The two original trades are cancelled. A new trade forms a new branch.
        fresh = self.fill(lots="3")
        self.assertEqual(self.symbol()["state"]["portfolio"]["long_lots"], 3)
        rollback(self.store, self.symbol_id, fresh["snapshot_id"], self.symbol()["revision"], uuid.uuid4().hex)
        self.assertEqual(self.symbol()["state"]["portfolio"]["long_lots"], 0)
        with self.store.read() as con:
            for old in (first, second, fresh):
                self.assertIsNotNone(con.execute(
                    "SELECT reverted_by FROM events WHERE id=?", (old["event_id"],)
                ).fetchone()[0])
        self.assertEqual(self.count("outbox", "kind='manual' AND status='pending'"), 0)

    def test_authenticated_get_pages_never_fetch_market_or_write_business_state(self):
        from app import create_app
        app = create_app(self.temporary.name)
        app.testing = True
        with self.store.tx() as con:
            self.store.set_setting(con, "auth", {"version": "test-version",
                                                  "password_hash": "unused-for-authenticated-get"})
        client = app.test_client()
        with client.session_transaction() as session:
            session["login_method"] = "password"
            session["auth_version"] = "test-version"
            session["csrf"] = "test-csrf"
        before = self.symbol()
        counts = {t: self.count(t) for t in ("events", "snapshots", "outbox", "requests", "price_history", "backtests")}
        with patch("market_data.collect_quotes", side_effect=AssertionError("GET must not fetch quotes")), \
                patch("runtime.process_quotes", side_effect=AssertionError("GET must not process quotes")):
            for path in ("/status", "/api/status", "/backtest", "/health"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
        self.assertEqual(self.symbol(), before)
        self.assertEqual({t: self.count(t) for t in counts}, counts)


if __name__ == "__main__":
    unittest.main()
