"""v1.2 regression tests: consolidated strategy switch, push icons, migration."""
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from config import SYSTEM_DEFAULTS, PUSH_DEFAULTS
from storage import Store
from contracts import normalize_contract
from service import add_symbol, save_config
from push import validate_config as push_validate
from push_format import quant_style_message
from jinja2 import Environment,FileSystemLoader

class V12Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name).initialize()

    def test_fresh_install_master_is_on_and_contract_does_not_have_off_state(self):
        with self.store.read() as con:
            self.assertTrue(self.store.setting(con,"system")["strategy_enabled"])
        symbol_id=add_symbol(self.store,normalize_contract("m2705"))
        with self.store.read() as con:
            symbol=self.store.symbol(con,symbol_id)
            self.assertTrue(symbol["config"]["enabled"])
        save_config(self.store,symbol_id,{"lower_price":"3000","upper_price":"3400","enabled":False},symbol["revision"])
        with self.store.read() as con:
            self.assertTrue(self.store.symbol(con,symbol_id)["config"]["enabled"])

    def test_previous_default_off_migrates_to_on(self):
        with self.store.tx() as con:
            self.store.set_meta(con,"master_switch_v12_migrated",None)
            con.execute("DELETE FROM metadata WHERE key='master_switch_v12_migrated'")
            old=self.store.setting(con,"system")
            old["strategy_enabled"]=False
            self.store.set_setting(con,"system",old)
        self.store.initialize()
        with self.store.read() as con:
            self.assertTrue(self.store.setting(con,"system")["strategy_enabled"])
            self.assertTrue(self.store.meta(con,"master_switch_v12_migrated"))

    def test_explicit_manually_closed_master_remains_off_on_upgrade(self):
        with self.store.tx() as con:
            con.execute("DELETE FROM metadata WHERE key='master_switch_v12_migrated'")
            old=self.store.setting(con,"system")
            old["strategy_enabled"]=False
            self.store.set_setting(con,"system",old)
            self.store.event(con,uuid.uuid4().hex,None,'settings','策略总开关','策略总开关已关闭')
        self.store.initialize()
        with self.store.read() as con:
            self.assertFalse(self.store.setting(con,"system")["strategy_enabled"])

    def test_ntfy_secret_hidden_and_clear_flag_ignored(self):
        old=dict(PUSH_DEFAULTS,NTFY_PASSWORD='supersecret')
        cfg=push_validate({'NTFY_PASSWORD':'','clear_ntfy_password':'1'},old)
        self.assertEqual(cfg['NTFY_PASSWORD'],'supersecret')
        cfg=push_validate({'NTFY_PASSWORD':'newsecret'},old)
        self.assertEqual(cfg['NTFY_PASSWORD'],'newsecret')

    def test_quant_style_every_line_has_icon_and_keeps_content(self):
        text=quant_style_message('买入提示 · 豆粕2705','合约：m2705\n观察价：3000\n买入线 2900 · 1 手\n行情：新浪期货 / 2026-11-08','signal')
        lines=text.splitlines()
        self.assertEqual(len(lines),5)
        for line in lines:
            self.assertTrue(ord(line[0])>255,line)
        self.assertIn('🎯[TRADE]',lines[0]);self.assertIn('📡行情',text)
        close=quant_style_message('🔴 策略总开关已关闭','策略总开关已手动关闭\n时间：2026','settings')
        self.assertIn('🔴[ERROR]',close)

    def test_enqueue_deduplicates_already_logged_switch(self):
        with self.store.tx() as con:
            self.store.enqueue(con,'off-1','策略总开关已关闭','时间：2026-11-08\n所有合约策略已关闭','settings',force=True)
            self.store.enqueue(con,'off-1','策略总开关已关闭','时间：2026-11-08\n所有合约策略已关闭','settings',force=True)
        with self.store.read() as con:
            rows=con.execute('SELECT * FROM outbox').fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['status'],'pending')
            self.assertEqual(rows[0]['force'],1)
            self.assertIn('🕒',rows[0]['body'])

    def test_templates_compile_and_expected_seven_panels(self):
        folder=Path(__file__).resolve().parents[1]/'templates'
        env=Environment(loader=FileSystemLoader(str(folder)))
        env.filters.update(dt=str,num=str,jsonpretty=str)
        for page in folder.glob('*.html'):
            with self.subTest(page=page.name):
                env.get_template(page.name)
        all_text='\n'.join(page.read_text() for page in folder.glob('*.html'))
        self.assertEqual(all_text.count('data-collapse-toggle'),7)
        self.assertNotIn('id="symbol-enabled"',all_text)
        self.assertNotIn('id="clear-ntfy-password"',all_text)
        self.assertNotIn('name="strategy_enabled"',all_text)

if __name__=='__main__':unittest.main()
