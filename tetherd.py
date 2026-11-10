#!/usr/bin/env python3
"""tetherd — keeps Android Ethernet tethering switched on, plus a Telegram bot.

Runs inside Termux on the phone itself (so the bot works over the phone's own 5G
even when the home internet is down). Standard library only.

    python tetherd.py            run forever (Termux:Boot starts this)
    python tetherd.py --check    print what it sees right now and exit
    python tetherd.py --enable   do one enable attempt right now and exit
    python tetherd.py --setup    interactive: bot token, auto chat id, root detection -> config.json
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("TETHERD_HOME", Path.home() / ".tetherd"))
REPO = Path(__file__).resolve().parent
METHODS = ("shell", "broadcast")
UI_DUMP = "/data/local/tmp/tetherd-ui.xml"


# ───────────────────────── config: parsed once, at the boundary ─────────────────────────
@dataclass(frozen=True)
class Config:
    token: str
    allowed_chat_ids: frozenset[int]
    method: str  # "shell": su/rish taps the switch itself; "broadcast": ask MacroDroid to do it
    shell: tuple[str, ...]
    broadcast_action: str
    settings_components: tuple[str, ...]
    switch_label: str
    iface_regex: str
    check_every_s: int
    boot_grace_s: int
    backoff_base_s: int
    backoff_max_s: int

    @property
    def idempotent(self) -> bool:
        # the shell method reads the switch before tapping; the broadcast method blindly toggles
        return self.method == "shell"


def parse_config(raw: dict) -> Config:
    method = raw.get("method", "broadcast")
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}, got {method!r}")
    token = str(raw.get("token", "")).strip()
    chats = frozenset(int(x) for x in raw.get("allowed_chat_ids", []))
    if token and not chats:
        raise ValueError("allowed_chat_ids is empty: nobody could use the bot (send /id to it to learn yours)")
    return Config(
        token=token,
        allowed_chat_ids=chats,
        method=method,
        shell=tuple(raw.get("shell", ["su", "-c"])),
        broadcast_action=raw.get("broadcast_action", "home.tether.ON"),
        settings_components=tuple(raw.get("settings_components", [
            "com.android.settings/.TetherSettings",
            "com.android.settings/.Settings$TetherSettingsActivity",
        ])),
        switch_label=raw.get("switch_label", r"(?i)ethernet"),
        iface_regex=raw.get("iface_regex", r"^(eth|usb|rndis|ncm)\d+"),
        check_every_s=int(raw.get("check_every_s", 30)),
        boot_grace_s=int(raw.get("boot_grace_s", 40)),
        backoff_base_s=int(raw.get("backoff_base_s", 60)),
        backoff_max_s=int(raw.get("backoff_max_s", 900)),
    )


def build_config(token: str, chat_id: int | None, rooted: bool) -> dict:
    """The minimal config.json the setup wizard writes; every other knob keeps its parse_config default."""
    return {"token": token, "allowed_chat_ids": [chat_id] if chat_id is not None else [],
            "method": "shell" if rooted else "broadcast"}


def load_config(path: Path) -> Config:
    return parse_config(json.loads(Path(path).read_text()))


# ───────────────────────── pure logic ─────────────────────────
@dataclass(frozen=True)
class Probe:
    up: bool | None  # None = could not determine
    detail: str


def probe_from_ip_output(text: str | None, iface_regex: str) -> Probe:
    """`ip -o -4 addr show` output -> is a tether interface carrying an IPv4 address?"""
    if not text:
        return Probe(None, "не удалось прочитать интерфейсы")
    rx = re.compile(iface_regex)
    for line in text.splitlines():
        m = re.match(r"\s*\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m and rx.match(m[1]):
            return Probe(True, f"{m[1]} {m[2]}")
    return Probe(False, "нет tether-интерфейса с адресом")


@dataclass
class Watch:
    attempts: int = 0
    last_attempt: float | None = None
    paused: bool = False
    last_up: bool | None = None


def should_enable(now: float, uptime: float, up: bool | None, w: Watch, cfg: Config, idempotent: bool) -> bool:
    if w.paused or up is True or uptime < cfg.boot_grace_s:
        return False
    if up is None and not idempotent and w.attempts > 0:
        return False  # can't see the state and the method toggles: act blind once per boot, never again
    if w.last_attempt is None:
        return True
    wait = min(cfg.backoff_base_s * 2 ** max(w.attempts - 1, 0), cfg.backoff_max_s)
    return now - w.last_attempt >= wait


def transition_message(was_up: bool | None, p: Probe) -> str | None:
    if p.up is True and was_up is not True:
        return f"✅ Раздача по Ethernet работает ({p.detail})"
    if p.up is False and was_up is True:
        return "⚠️ Раздача пропала — пробую включить"
    return None


@dataclass(frozen=True)
class Cmd:
    chat_id: int
    name: str


def parse_update(upd: dict, allowed: frozenset[int] | set[int], not_before: float) -> Cmd | None:
    msg = upd.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat is None or not text.startswith("/") or msg.get("date", 0) < not_before:
        return None
    name = text.split()[0][1:].split("@")[0].lower()
    if chat not in allowed and name != "id":
        return None
    return Cmd(chat, name)


def chat_id_from_updates(updates: list[dict]) -> int | None:
    chats = [m["chat"]["id"] for u in updates if (m := u.get("message"))
             and m.get("chat", {}).get("type") == "private"]
    return chats[-1] if chats else None


def _center(bounds: str) -> tuple[int, int]:
    x1, y1, x2, y2 = map(int, re.findall(r"-?\d+", bounds))
    return (x1 + x2) // 2, (y1 + y2) // 2


def find_switch(xml_text: str, label_regex: str) -> tuple[bool | None, tuple[int, int]] | None:
    """uiautomator dump -> (is the switch on?, where to tap its row) for the first row whose text matches."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    parent = {c: p for p in root.iter() for c in p}
    rx = re.compile(label_regex)
    for node in root.iter("node"):
        if not rx.search(node.get("text", "")):
            continue
        row = node
        while row is not None and row.get("clickable") != "true":
            row = parent.get(row)
        row = node if row is None else row
        checked = next((n.get("checked") == "true" for n in row.iter("node") if n.get("checkable") == "true"), None)
        return checked, _center(row.get("bounds", "[0,0][0,0]"))
    return None


# ───────────────────────── side effects ─────────────────────────
@dataclass(frozen=True)
class Result:
    ok: bool
    out: str


def run(cmd: list[str], timeout: int = 20) -> Result:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return Result(p.returncode == 0, (p.stdout + p.stderr).strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        return Result(False, str(e))


class Log:
    def __init__(self, path: Path, max_bytes: int = 512_000):
        self.path, self.max = path, max_bytes

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        with self.path.open("a") as f:
            f.write(line + "\n")
        if self.path.stat().st_size > self.max:
            self.path.write_text("".join(self.path.read_text().splitlines(True)[-2000:]))

    def tail(self, n: int = 20) -> str:
        try:
            return "".join(self.path.read_text().splitlines(True)[-n:]) or "(пусто)"
        except OSError:
            return "(лога нет)"


def uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic()


def sh(cfg: Config, command: str, timeout: int = 20) -> Result:
    return run([*cfg.shell, command], timeout)


def probe(cfg: Config) -> Probe:
    r = run(["ip", "-o", "-4", "addr", "show"])
    if not r.ok and cfg.method == "shell":
        r = sh(cfg, "ip -o -4 addr show")
    return probe_from_ip_output(r.out if r.ok else None, cfg.iface_regex)


def enable_via_shell(cfg: Config) -> str:
    sh(cfg, "input keyevent KEYCODE_WAKEUP")
    sh(cfg, "wm dismiss-keyguard")
    opened = next((c for c in cfg.settings_components
                   if (r := sh(cfg, f"am start -W -n '{c}'")).ok and "Error" not in r.out), None)
    if opened is None:
        return "не смог открыть настройки модема (проверь settings_components)"
    time.sleep(2.5)
    dump = sh(cfg, f"uiautomator dump {UI_DUMP} >/dev/null && cat {UI_DUMP}", timeout=30)
    found = find_switch(dump.out[dump.out.find("<"):], cfg.switch_label)
    if found is None:
        sh(cfg, "input keyevent KEYCODE_HOME")
        return f"переключатель «{cfg.switch_label}» не найден на экране {opened}"
    checked, (x, y) = found
    if checked is not True:
        sh(cfg, f"input tap {x} {y}")
    time.sleep(1.5)
    sh(cfg, "input keyevent KEYCODE_HOME")
    return "уже был включён" if checked else f"нажал переключатель ({x},{y})"


def enable(cfg: Config) -> str:
    if cfg.method == "shell":
        return enable_via_shell(cfg)
    r = run(["am", "broadcast", "-a", cfg.broadcast_action])
    return "отправил интент MacroDroid" if r.ok else f"am broadcast упал: {r.out[:200]}"


def battery() -> str:
    r = run(["termux-battery-status"], timeout=10)
    try:
        b = json.loads(r.out)
        return f"{b['percentage']}% {b.get('status', '').lower()}, {b.get('temperature', 0):.0f}°C"
    except (ValueError, KeyError, TypeError):
        return "н/д (нужен Termux:API)"


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, params: dict, http_timeout: float = 15):
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(self.base + method, data, timeout=http_timeout) as r:
            j = json.load(r)
        if not j.get("ok"):
            raise RuntimeError(str(j)[:200])
        return j["result"]


def fmt_age(s: float) -> str:
    d, s = divmod(int(s), 86400)
    h, s = divmod(s, 3600)
    return f"{d}д {h}ч {s // 60}м" if d else f"{h}ч {s // 60}м"


HELP = ("/status — состояние\n/tether — включить раздачу сейчас\n/pause — не трогать раздачу\n"
        "/resume — снова следить\n/log — последние строки лога\n/update — git pull и перезапуск\n/reboot — перезагрузить (только root)\n/id — твой chat id")


# ───────────────────────── the daemon ─────────────────────────
@dataclass
class Daemon:
    cfg: Config
    log: Log
    started: float = field(default_factory=time.time)
    w: Watch = field(default_factory=Watch)
    outbox: list[str] = field(default_factory=list)
    last_probe: Probe = Probe(None, "ещё не проверял")

    def notify(self, text: str) -> None:
        self.log(f"notify: {text}")
        self.outbox.append(text)

    def attempt(self, reason: str) -> None:
        self.w.attempts += 1
        self.w.last_attempt = time.monotonic()
        self.log(f"enable #{self.w.attempts} ({reason}): {enable(self.cfg)}")

    def tick(self) -> None:
        p = probe(self.cfg)
        if (m := transition_message(self.w.last_up, p)) and not self.w.paused:
            self.notify(m)
        if p.up is True:
            self.w.attempts, self.w.last_attempt = 0, None
        self.w.last_up, self.last_probe = p.up, p
        if should_enable(time.monotonic(), uptime(), p.up, self.w, self.cfg, self.cfg.idempotent):
            self.attempt(p.detail)
            if self.w.attempts == 4:
                self.notify("❌ 4 попытки не помогли. Проверь кабель/адаптер, /log покажет детали")

    def handle(self, c: Cmd) -> str:
        self.log(f"command /{c.name} from {c.chat_id}")
        if c.name == "id":
            return f"Твой chat id: {c.chat_id}"
        if c.name == "status":
            p = self.last_probe
            state = {True: "✅ работает", False: "❌ выключена", None: "❓ не вижу"}[p.up]
            paused = "\n⏸ На паузе" if self.w.paused else ""
            return (f"Раздача: {state} ({p.detail})\nUptime: {fmt_age(uptime())}\nБатарея: {battery()}\n"
                    f"Попыток подряд: {self.w.attempts}\nМетод: {self.cfg.method}{paused}")
        if c.name == "tether":
            self.w.paused = False
            self.attempt("команда /tether")
            return f"Пробую. {self.log.tail(1).strip()}"
        if c.name == "pause":
            self.w.paused = True
            return "⏸ Не трогаю раздачу до /resume"
        if c.name == "resume":
            self.w.paused, self.w.attempts, self.w.last_attempt = False, 0, None
            return "▶️ Снова слежу"
        if c.name == "log":
            return self.log.tail(25)[-3900:]
        if c.name == "update":
            r = run(["git", "-C", str(REPO), "pull", "--ff-only"], timeout=60)
            return f"{'⬆️' if r.ok else '❌'} {r.out[-1500:]}" + ("\nПерезапускаюсь…" if r.ok else "")
        if c.name == "reboot":
            return "🔁 Перезагружаю…" if self.cfg.method == "shell" else "Нужен root (method=shell)"
        return HELP

    def serve(self) -> None:
        tg = Telegram(self.cfg.token) if self.cfg.token else None
        offset_file = HOME / "offset"
        offset = int(offset_file.read_text()) if offset_file.exists() else 0
        self.notify(f"🔄 tetherd запущен (uptime {fmt_age(uptime())})")
        while True:
            try:
                self.tick()
            except Exception as e:  # never let one bad tick kill the watchdog
                self.log(f"tick error: {e!r}")
            if tg is None:
                self.outbox.clear()
                time.sleep(self.cfg.check_every_s)
                continue
            try:
                while self.outbox:
                    for chat in self.cfg.allowed_chat_ids:
                        tg.call("sendMessage", {"chat_id": chat, "text": self.outbox[0]})
                    self.outbox.pop(0)
                updates = tg.call("getUpdates", {"offset": offset, "timeout": self.cfg.check_every_s},
                                  http_timeout=self.cfg.check_every_s + 15)
                for u in updates:
                    offset = u["update_id"] + 1
                    offset_file.write_text(str(offset))  # ack before acting, so /reboot can't loop
                    if (c := parse_update(u, self.cfg.allowed_chat_ids, self.started - 5)) is None:
                        continue
                    reply = self.handle(c)
                    tg.call("sendMessage", {"chat_id": c.chat_id, "text": reply})
                    if c.name == "update" and reply.startswith("⬆️"):
                        tg.call("getUpdates", {"offset": offset, "timeout": 0})
                        self.log("restarting after update")
                        os._exit(0)  # start-tetherd's loop brings us back on the new code
                    if c.name == "reboot" and self.cfg.method == "shell":
                        tg.call("getUpdates", {"offset": offset, "timeout": 0})  # confirm ack server-side
                        self.log("rebooting by command")
                        sh(self.cfg, "svc power reboot || reboot")
            except Exception as e:  # no internet / Telegram down: keep the watchdog running
                self.log(f"telegram: {e!r}")
                time.sleep(self.cfg.check_every_s)


def setup() -> int:
    print("Настройка tetherd. Enter без токена — работать без Telegram-бота.")
    token = input("Токен бота от @BotFather: ").strip()
    chat_id = None
    if token:
        tg = Telegram(token)
        try:
            name = tg.call("getMe", {})["username"]
        except Exception as e:
            print(f"Токен не подошёл: {e}")
            return 1
        print(f"Бот @{name}. Открой его в Telegram и напиши что угодно (жду до 5 минут)…")
        offset, deadline = 0, time.time() + 300
        while chat_id is None and time.time() < deadline:
            ups = tg.call("getUpdates", {"offset": offset, "timeout": 25}, http_timeout=40)
            offset = ups[-1]["update_id"] + 1 if ups else offset
            chat_id = chat_id_from_updates(ups)
        if chat_id is None:
            print("Не дождался сообщения. Запусти --setup ещё раз.")
            return 1
        tg.call("getUpdates", {"offset": offset, "timeout": 0})
        tg.call("sendMessage", {"chat_id": chat_id, "text": "✅ Подключено. /help — команды"})
    rooted = run(["su", "-c", "id"], timeout=30).ok
    print(f"chat id: {chat_id}  root: {'есть → method=shell' if rooted else 'нет → method=broadcast (MacroDroid)'}")
    (HOME / "config.json").write_text(json.dumps(build_config(token, chat_id, rooted), indent=2))
    print(f"Сохранено в {HOME / 'config.json'}")
    return 0


def main(argv: list[str]) -> int:
    HOME.mkdir(parents=True, exist_ok=True)
    if "--setup" in argv:
        return setup()
    cfg = load_config(HOME / "config.json")
    log = Log(HOME / "tetherd.log")
    if "--check" in argv:
        print(f"method={cfg.method} bot={'on' if cfg.token else 'off'} uptime={fmt_age(uptime())}")
        print("probe:", probe(cfg))
        print("battery:", battery())
        return 0
    if "--enable" in argv:
        print(enable(cfg))
        return 0
    lock = (HOME / "lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # exactly one daemon at a time
    except BlockingIOError:
        print("tetherd уже запущен", file=sys.stderr)
        return 1
    Daemon(cfg, log).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
