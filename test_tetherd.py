"""Behaviour tests for tetherd's pure logic. Run: python3 -m unittest -v test_tetherd"""
import json
import tempfile
import unittest
from pathlib import Path

import tetherd as t

IP_UP = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
25: rmnet_data2    inet 10.123.4.5/30 scope global rmnet_data2\\       valid_lft forever preferred_lft forever
30: eth0    inet 192.168.143.1/24 brd 192.168.143.255 scope global eth0\\       valid_lft forever preferred_lft forever
"""
IP_DOWN = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
25: rmnet_data2    inet 10.123.4.5/30 scope global rmnet_data2\\       valid_lft forever preferred_lft forever
"""

UI_OFF = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
 <node text="" clickable="false" bounds="[0,0][1080,2400]">
  <node text="" clickable="true" checkable="false" bounds="[0,500][1080,700]">
   <node text="USB tethering" clickable="false" bounds="[50,520][800,600]"/>
   <node text="" checkable="true" checked="false" clickable="false" bounds="[900,550][1030,650]"/>
  </node>
  <node text="" clickable="true" checkable="false" bounds="[0,700][1080,900]">
   <node text="Ethernet tethering" clickable="false" bounds="[50,720][800,800]"/>
   <node text="Share phone's internet connection via Ethernet" clickable="false" bounds="[50,800][800,880]"/>
   <node text="" checkable="true" checked="false" clickable="false" bounds="[900,750][1030,850]"/>
  </node>
 </node>
</hierarchy>"""
UI_ON = UI_OFF.replace(
    'checked="false" clickable="false" bounds="[900,750]', 'checked="true" clickable="false" bounds="[900,750]'
)


def cfg(**over):
    base = dict(token="", allowed_chat_ids=[1], method="broadcast")
    base.update(over)
    return t.parse_config(base)


class ProbeTest(unittest.TestCase):
    def test_tether_interface_with_ipv4_is_up(self):
        p = t.probe_from_ip_output(IP_UP, r"^(eth|usb|rndis|ncm)\d+")
        self.assertIs(p.up, True)
        self.assertIn("eth0", p.detail)
        self.assertIn("192.168.143.1", p.detail)

    def test_only_mobile_data_means_down(self):
        self.assertIs(t.probe_from_ip_output(IP_DOWN, r"^(eth|usb)\d+").up, False)

    def test_no_output_means_unknown(self):
        self.assertIsNone(t.probe_from_ip_output(None, r"^eth\d+").up)


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.c = cfg(boot_grace_s=40, backoff_base_s=60, backoff_max_s=900)

    def decide(self, up, uptime=1000, now=10_000.0, attempts=0, last=None, idempotent=False, paused=False):
        w = t.Watch(attempts=attempts, last_attempt=last, paused=paused)
        return t.should_enable(now, uptime, up, w, self.c, idempotent)

    def test_up_never_enables(self):
        self.assertFalse(self.decide(True))

    def test_down_enables_after_boot_grace(self):
        self.assertTrue(self.decide(False, uptime=41))

    def test_down_waits_during_boot_grace(self):
        self.assertFalse(self.decide(False, uptime=10))

    def test_paused_never_enables(self):
        self.assertFalse(self.decide(False, paused=True))

    def test_backoff_doubles_and_caps(self):
        # after 1st attempt wait 60s, after 2nd 120s, ... capped at 900s
        self.assertFalse(self.decide(False, attempts=1, last=10_000 - 59))
        self.assertTrue(self.decide(False, attempts=1, last=10_000 - 60))
        self.assertFalse(self.decide(False, attempts=2, last=10_000 - 119))
        self.assertTrue(self.decide(False, attempts=2, last=10_000 - 120))
        self.assertTrue(self.decide(False, attempts=20, last=10_000 - 900))

    def test_unknown_state_with_toggle_method_acts_blind_only_once(self):
        self.assertTrue(self.decide(None, attempts=0))
        self.assertFalse(self.decide(None, attempts=1, last=0))

    def test_unknown_state_with_idempotent_method_keeps_trying(self):
        self.assertTrue(self.decide(None, attempts=3, last=0, idempotent=True))


class TransitionTest(unittest.TestCase):
    def test_messages_only_on_change(self):
        self.assertIsNone(t.transition_message(True, t.Probe(True, "eth0")))
        self.assertIn("✅", t.transition_message(False, t.Probe(True, "eth0")))
        self.assertIn("✅", t.transition_message(None, t.Probe(True, "eth0")))
        self.assertIn("⚠️", t.transition_message(True, t.Probe(False, "")))
        self.assertIsNone(t.transition_message(None, t.Probe(False, "")))


class CommandTest(unittest.TestCase):
    def upd(self, text, chat=1, date=2000):
        return {"update_id": 5, "message": {"chat": {"id": chat}, "date": date, "text": text}}

    def test_allowed_chat_command(self):
        self.assertEqual(t.parse_update(self.upd("/status"), {1}, 1000), t.Cmd(1, "status"))

    def test_bot_suffix_and_args_stripped(self):
        self.assertEqual(t.parse_update(self.upd("/Tether@my_bot now"), {1}, 1000), t.Cmd(1, "tether"))

    def test_stranger_gets_only_id(self):
        self.assertIsNone(t.parse_update(self.upd("/reboot", chat=666), {1}, 1000))
        self.assertEqual(t.parse_update(self.upd("/id", chat=666), {1}, 1000), t.Cmd(666, "id"))

    def test_messages_from_before_start_are_dropped(self):
        # prevents a /reboot sent earlier from rebooting the phone again after boot
        self.assertIsNone(t.parse_update(self.upd("/reboot", date=999), {1}, 1000))

    def test_non_commands_ignored(self):
        self.assertIsNone(t.parse_update(self.upd("hello"), {1}, 1000))
        self.assertIsNone(t.parse_update({"update_id": 1, "edited_message": {}}, {1}, 1000))


class UiTest(unittest.TestCase):
    def test_finds_row_state_and_center(self):
        self.assertEqual(t.find_switch(UI_OFF, r"(?i)ethernet"), (False, (540, 800)))
        self.assertEqual(t.find_switch(UI_ON, r"(?i)ethernet"), (True, (540, 800)))

    def test_missing_label(self):
        self.assertIsNone(t.find_switch(UI_OFF, r"(?i)bluetooth"))

    def test_garbage_xml(self):
        self.assertIsNone(t.find_switch("ERROR: null root node", r"(?i)ethernet"))


class ConfigTest(unittest.TestCase):
    def test_rejects_unknown_method(self):
        with self.assertRaises(ValueError):
            cfg(method="magic")

    def test_bot_needs_allowed_chats(self):
        with self.assertRaises(ValueError):
            cfg(token="123:abc", allowed_chat_ids=[])

    def test_loads_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"token": "", "allowed_chat_ids": ["42"], "method": "shell"}))
            c = t.load_config(p)
            self.assertEqual(c.allowed_chat_ids, frozenset({42}))
            self.assertEqual(c.method, "shell")


class SetupTest(unittest.TestCase):
    def test_chat_id_is_taken_from_latest_private_message(self):
        ups = [
            {"update_id": 1, "message": {"chat": {"id": -100, "type": "group"}, "text": "hi"}},
            {"update_id": 2, "message": {"chat": {"id": 42, "type": "private"}, "text": "hi"}},
            {"update_id": 3, "edited_message": {"chat": {"id": 7, "type": "private"}}},
        ]
        self.assertEqual(t.chat_id_from_updates(ups), 42)

    def test_no_private_message_yet(self):
        self.assertIsNone(t.chat_id_from_updates([]))

    def test_built_config_round_trips_through_parser(self):
        c = t.parse_config(t.build_config("1:abc", 42, rooted=True))
        self.assertEqual((c.token, c.allowed_chat_ids, c.method), ("1:abc", frozenset({42}), "shell"))
        self.assertEqual(t.parse_config(t.build_config("", None, rooted=False)).method, "broadcast")


if __name__ == "__main__":
    unittest.main()
