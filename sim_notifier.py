#!/usr/bin/env python3
"""USB ADB SIM Call & SMS Discord Notifier for Huawei BG2-U01 (Android 7.0)."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.env"
DEFAULT_STATE_PATH = BASE_DIR / "state.json"
SERVICE_NAME = "sim-adb-notifier.service"

DEFAULT_SMS_PACKAGES = frozenset(
    {
        "com.android.mms",
        "com.android.mms.service",
        "com.android.cellbroadcastreceiver",
        "com.android.stk",
        "com.huawei.omacp",
        "com.google.android.apps.messaging",
        "org.fossify.messages",
    }
)
CALL_MISSED_PACKAGES = frozenset(
    {
        "com.android.server.telecom",
        "com.android.phone",
        "com.android.contacts",
    }
)
INCALL_PACKAGE = "com.android.incallui"
EVENT_WATCH_PACKAGES = (
    "com.android.mms",
    "com.android.phone",
    "com.android.incallui",
    "com.android.server.telecom",
)

ONGOING_FLAGS_MASK = 0x42  # FLAG_ONGOING_EVENT (0x2) | FLAG_FOREGROUND_SERVICE (0x40)
NOTIF_SPLIT_MARKER = "===NOTIF_SPLIT==="

COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_BLUE = 0x3498DB
COLOR_AMBER = 0xF39C12
COLOR_PURPLE = 0x9B59B6

CALL_STATE_NAMES = {
    0: "IDLE",
    1: "RINGING",
    2: "OFFHOOK",
}

DISCORD_WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?$"
)


class AdbTransportError(RuntimeError):
    """Raised when the USB ADB device is unreachable or returns an error."""


@dataclass
class Config:
    """Runtime configuration loaded from config.env."""

    discord_webhook_url: str = ""
    discord_mention: str = ""
    adb_serial: str = ""
    poll_interval_seconds: float = 2.5
    alert_on_disconnect: bool = True
    extra_watch_packages: frozenset[str] = field(default_factory=frozenset)


@dataclass
class NotifRecord:
    """Parsed active Android notification from dumpsys notification --noredact."""

    pkg: str
    key: str
    flags: int
    ticker_text: str | None
    creation_ms: int
    update_ms: int
    extras: dict[str, str | None]


@dataclass
class AlertEvent:
    """Structured alert ready to be formatted and sent to Discord."""

    title: str
    description: str
    color: int
    fields: list[dict[str, object]] = field(default_factory=list)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """Parse KEY=VALUE configuration file, returning defaults if absent."""
    raw: dict[str, str] = {}
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            raw[key.strip()] = _strip_quotes(val.strip())

    try:
        poll = max(0.5, float(raw.get("POLL_INTERVAL_SECONDS") or 2.5))
    except ValueError:
        poll = 2.5

    alert_disc_raw = (raw.get("ALERT_ON_DISCONNECT") or "true").strip().lower()
    alert_on_disconnect = alert_disc_raw in ("1", "true", "yes", "on")

    extra_raw = raw.get("EXTRA_WATCH_PACKAGES", "")
    extra_pkgs = frozenset(p.strip() for p in extra_raw.split(",") if p.strip())

    return Config(
        discord_webhook_url=raw.get("DISCORD_WEBHOOK_URL", "").strip(),
        discord_mention=raw.get("DISCORD_MENTION", "").strip(),
        adb_serial=raw.get("ADB_SERIAL", "").strip(),
        poll_interval_seconds=poll,
        alert_on_disconnect=alert_on_disconnect,
        extra_watch_packages=extra_pkgs,
    )


def update_config_file(
    config_path: Path, updates: dict[str, str], chmod_600: bool = True
) -> None:
    """Update or append keys in config.env while preserving comments and order."""
    lines = (
        config_path.read_text(encoding="utf-8").splitlines()
        if config_path.exists()
        else []
    )
    remaining = dict(updates)
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                continue
        new_lines.append(line)

    for key, val in remaining.items():
        new_lines.append(f"{key}={val}")

    config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    if chmod_600:
        os.chmod(config_path, 0o600)


def _adb_base_cmd(serial: str) -> list[str]:
    return ["adb", "-s", serial] if serial else ["adb", "-d"]


def run_adb_snapshot(serial: str, timeout: float = 10.0) -> tuple[str, str]:
    """Fetch telephony.registry and notification --noredact dumps over USB ADB."""
    cmd = _adb_base_cmd(serial) + [
        "shell",
        f"dumpsys telephony.registry; echo {NOTIF_SPLIT_MARKER}; dumpsys notification --noredact",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbTransportError(f"adb snapshot timed out after {timeout}s") from exc
    except OSError as exc:
        raise AdbTransportError(f"failed to invoke adb: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise AdbTransportError(detail)

    if NOTIF_SPLIT_MARKER not in proc.stdout:
        detail = (proc.stderr or proc.stdout[:200]).strip()
        raise AdbTransportError(f"incomplete adb snapshot output: {detail}")

    telephony_raw, notif_raw = proc.stdout.split(NOTIF_SPLIT_MARKER, 1)
    return telephony_raw, notif_raw


class EventsWakeThread(threading.Thread):
    """Background logcat -b events reader that wakes polling on telephony/MMS activity."""

    def __init__(
        self, serial: str, wake_event: threading.Event, stop_event: threading.Event
    ) -> None:
        super().__init__(name="events-wake", daemon=True)
        self.serial = serial
        self.wake_event = wake_event
        self.stop_event = stop_event
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def terminate_proc(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    def run(self) -> None:
        cmd = _adb_base_cmd(self.serial) + ["logcat", "-b", "events", "-T", "1"]
        while not self.stop_event.is_set():
            proc: subprocess.Popen[str] | None = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                with self._lock:
                    self._proc = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    if self.stop_event.is_set():
                        break
                    if any(pkg in line for pkg in EVENT_WATCH_PACKAGES):
                        self.wake_event.set()
            except OSError:
                pass
            finally:
                with self._lock:
                    self._proc = None
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            if not self.stop_event.is_set():
                self.stop_event.wait(5.0)


def _extract_carrier_and_voice(service_line: str) -> tuple[str, str]:
    """Extract carrier name and voice registration state from mServiceState line."""
    voice_state = "unknown"
    carrier = "Unknown"

    m_voice = re.search(
        r"\bvoice\s+(emergency only|out of service|\S+)\s+data\s+(?:emergency only|out of service|\S+)\s+(.*)$",
        service_line,
    )
    if m_voice:
        voice_state = m_voice.group(1).strip()
        tail = m_voice.group(2).strip()
        m_plmn = re.match(r"^(.*?)\s+\d{5,6}\b", tail)
        if m_plmn:
            alpha_pair = m_plmn.group(1).strip()
            parts = alpha_pair.split()
            if len(parts) >= 2 and len(parts) % 2 == 0:
                half = len(parts) // 2
                if parts[:half] == parts[half:]:
                    alpha_pair = " ".join(parts[:half])
            if alpha_pair and alpha_pair.lower() != "null":
                carrier = alpha_pair
    return carrier, voice_state


def parse_telephony(text: str) -> dict[str, object]:
    """Parse Android 7.0 dumpsys telephony.registry into call and SIM state."""
    call_states = [int(m.group(1)) for m in re.finditer(r"^\s*mCallState=(\d+)", text, re.M)]
    incoming_nums = [
        m.group(1).strip()
        for m in re.finditer(r"^\s*mCallIncomingNumber=(.*)$", text, re.M)
    ]

    selected_idx = 0
    for idx, cs in enumerate(call_states):
        if cs != 0:
            selected_idx = idx
            break

    call_state = call_states[selected_idx] if call_states else 0
    incoming_number = (
        incoming_nums[selected_idx] if selected_idx < len(incoming_nums) else ""
    )

    m_service = re.search(r"^\s*mServiceState=(.*)$", text, re.M)
    carrier, voice_state = (
        _extract_carrier_and_voice(m_service.group(1))
        if m_service
        else ("Unknown", "unknown")
    )

    m_sig = re.search(r"^\s*mSignalStrength=SignalStrength:\s*(-?\d+)", text, re.M)
    signal_dbm: int | None = int(m_sig.group(1)) if m_sig else None

    return {
        "call_state": call_state,
        "incoming_number": incoming_number,
        "carrier": carrier,
        "voice_state": voice_state,
        "signal_dbm": signal_dbm,
    }


def _unwrap_extra_value(raw: str) -> str | None:
    if raw == "null":
        return None
    m = re.match(r"^(?:String|SpannableString|CharSequence)\s*\(([\s\S]*)\)$", raw)
    if m:
        inner = m.group(1)
        return None if inner == "null" else inner
    return raw


def _parse_extras_block(block: str) -> dict[str, str | None]:
    m_extras = re.search(r"\n      extras=\{\r?\n([\s\S]*?)\r?\n      \}", block)
    if not m_extras:
        return {}

    extras_raw = m_extras.group(1)
    entries: list[tuple[str, str]] = []
    for line in extras_raw.splitlines():
        m_kv = re.match(r"^        ([A-Za-z0-9_.]+)=(.*)$", line)
        if m_kv:
            entries.append((m_kv.group(1), m_kv.group(2)))
        elif entries:
            prev_k, prev_v = entries[-1]
            entries[-1] = (prev_k, f"{prev_v}\n{line}")

    return {k: _unwrap_extra_value(v) for k, v in entries}


def parse_notifications(text: str) -> list[NotifRecord]:
    """Parse active notifications from Android 7.0 dumpsys notification --noredact."""
    active_part = re.split(r"\r?\n  mArchive=", text, maxsplit=1)[0]
    active_part = re.split(r"\r?\n  mUseAttentionLight=", active_part, maxsplit=1)[0]

    chunks = re.split(r"(?m)^    NotificationRecord\(", active_part)
    records: list[NotifRecord] = []

    for chunk in chunks[1:]:
        m_pkg = re.search(r"\bpkg=([^\s]+)", chunk)
        if not m_pkg:
            continue
        pkg = m_pkg.group(1)

        m_key = re.search(r"^      key=(.+)$", chunk, re.M)
        if not m_key:
            m_key = re.search(r"\bkey=([^\s:]+)", chunk)
        key = m_key.group(1).strip() if m_key else pkg

        m_flags = re.search(r"\bflags=0x([0-9a-fA-F]+)", chunk)
        flags = int(m_flags.group(1), 16) if m_flags else 0

        m_ticker = re.search(
            r"\r?\n      tickerText=([\s\S]*?)\r?\n      contentView=", chunk
        )
        ticker_text: str | None = None
        if m_ticker:
            raw_ticker = m_ticker.group(1).strip()
            ticker_text = None if raw_ticker == "null" else raw_ticker

        m_create = re.search(r"^\s+mCreationTimeMs=(\d+)", chunk, re.M)
        m_update = re.search(r"^\s+mUpdateTimeMs=(\d+)", chunk, re.M)
        creation_ms = int(m_create.group(1)) if m_create else 0
        update_ms = int(m_update.group(1)) if m_update else creation_ms

        extras = _parse_extras_block(chunk)
        records.append(
            NotifRecord(
                pkg=pkg,
                key=key,
                flags=flags,
                ticker_text=ticker_text,
                creation_ms=creation_ms,
                update_ms=update_ms,
                extras=extras,
            )
        )

    return records


def extract_sms_content(notif: NotifRecord) -> tuple[str, str]:
    """Extract (sender, body) handling single SMS and Huawei InboxStyle grouped SMS."""
    extras = notif.extras
    if extras.get("android.textLines") is not None and notif.ticker_text:
        if ": " in notif.ticker_text:
            sender, body = notif.ticker_text.split(": ", 1)
        else:
            sender = extras.get("android.title") or "SMS"
            body = notif.ticker_text
    else:
        sender = extras.get("android.title") or "Unknown Sender"
        body = (
            extras.get("android.bigText")
            or extras.get("android.text")
            or notif.ticker_text
            or "(Empty message)"
        )
    return sender.strip(), body.strip()


def _resolve_caller_from_snapshot(
    telephony: dict[str, object], notifs: list[NotifRecord]
) -> str:
    number = str(telephony.get("incoming_number") or "").strip()
    if number:
        return number
    for notif in notifs:
        if notif.pkg == INCALL_PACKAGE:
            title = (notif.extras.get("android.title") or "").strip()
            text = (notif.extras.get("android.text") or "").strip()
            if title and text and title.lower() not in text.lower():
                return f"{title} ({text})"
            if title:
                return title
            if text:
                return text
    return ""


def _sim_summary_field(telephony: dict[str, object]) -> dict[str, object]:
    carrier = str(telephony.get("carrier") or "Unknown")
    voice_state = str(telephony.get("voice_state") or "unknown")
    sig = telephony.get("signal_dbm")
    sig_str = f"{sig} dBm" if sig is not None else "n/a"
    return {
        "name": "SIM / Signal",
        "value": f"{carrier} ({voice_state}, {sig_str})",
        "inline": True,
    }


@dataclass
class NotifierState:
    """Persistent state serialized to state.json across restarts."""

    initialized: bool = False
    prev_call_state: int = 0
    active_call_number: str = ""
    call_ring_start_ts: float | None = None
    call_was_answered: bool = False
    last_missed_call_ts: float = 0.0
    seen_notifs: dict[str, dict[str, object]] = field(default_factory=dict)
    recent_sms_hashes: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> NotifierState:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            initialized=bool(data.get("initialized", True)),
            prev_call_state=int(data.get("prev_call_state", 0)),
            active_call_number=str(data.get("active_call_number", "")),
            call_ring_start_ts=data.get("call_ring_start_ts"),
            call_was_answered=bool(data.get("call_was_answered", False)),
            last_missed_call_ts=float(data.get("last_missed_call_ts", 0.0)),
            seen_notifs=dict(data.get("seen_notifs", {})),
            recent_sms_hashes=dict(data.get("recent_sms_hashes", {})),
        )

    def save(self, path: Path) -> None:
        payload = {
            "initialized": self.initialized,
            "prev_call_state": self.prev_call_state,
            "active_call_number": self.active_call_number,
            "call_ring_start_ts": self.call_ring_start_ts,
            "call_was_answered": self.call_was_answered,
            "last_missed_call_ts": self.last_missed_call_ts,
            "seen_notifs": self.seen_notifs,
            "recent_sms_hashes": self.recent_sms_hashes,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)


def _prune_recent_sms(state: NotifierState, now_ts: float) -> None:
    cutoff = now_ts - 300.0
    state.recent_sms_hashes = {
        k: v
        for k, v in state.recent_sms_hashes.items()
        if float(v.get("ts", 0.0)) >= cutoff
    }


def process_snapshot(
    state: NotifierState,
    telephony: dict[str, object],
    notifs: list[NotifRecord],
    watch_sms_packages: frozenset[str],
    now_ts: float | None = None,
) -> list[AlertEvent]:
    """Advance state machine on a snapshot and return alerts to dispatch."""
    ts = time.time() if now_ts is None else now_ts
    alerts: list[AlertEvent] = []
    call_state = int(telephony.get("call_state") or 0)

    current_seen = {
        n.key: {
            "pkg": n.pkg,
            "creation_ms": n.creation_ms,
            "update_ms": n.update_ms,
        }
        for n in notifs
    }

    # First run ever: baseline active notifications without spamming old items.
    if not state.initialized:
        state.initialized = True
        state.seen_notifs = current_seen
        if call_state == 0:
            state.prev_call_state = 0
            return alerts

    # 1. Call state machine
    if call_state == 1:
        caller = _resolve_caller_from_snapshot(telephony, notifs) or "Unknown Number"
        if state.prev_call_state != 1:
            state.call_ring_start_ts = ts
            state.call_was_answered = False
            state.active_call_number = caller
            alerts.append(
                AlertEvent(
                    title="📞 Incoming Call",
                    description=f"Incoming call on tablet SIM from **{caller}**",
                    color=COLOR_GREEN,
                    fields=[
                        {"name": "Caller", "value": caller, "inline": True},
                        _sim_summary_field(telephony),
                    ],
                )
            )
        elif state.active_call_number == "Unknown Number" and caller != "Unknown Number":
            state.active_call_number = caller
    elif call_state == 2:
        state.call_was_answered = True
        caller = _resolve_caller_from_snapshot(telephony, notifs)
        if caller and (
            not state.active_call_number or state.active_call_number == "Unknown Number"
        ):
            state.active_call_number = caller
    elif call_state == 0 and state.prev_call_state in (1, 2):
        if not state.call_was_answered:
            caller = state.active_call_number or "Unknown Number"
            ring_secs = max(0.0, ts - (state.call_ring_start_ts or ts))
            state.last_missed_call_ts = ts
            alerts.append(
                AlertEvent(
                    title="📵 Missed Call",
                    description=f"Missed call from **{caller}**",
                    color=COLOR_RED,
                    fields=[
                        {"name": "Caller", "value": caller, "inline": True},
                        {
                            "name": "Ring Duration",
                            "value": f"{ring_secs:.1f}s",
                            "inline": True,
                        },
                        _sim_summary_field(telephony),
                    ],
                )
            )
        state.active_call_number = ""
        state.call_ring_start_ts = None
        state.call_was_answered = False

    state.prev_call_state = call_state

    # 2. Notification watcher (SMS + flash missed-call fallback)
    _prune_recent_sms(state, ts)
    for notif in notifs:
        if notif.flags & ONGOING_FLAGS_MASK:
            continue

        prev = state.seen_notifs.get(notif.key)
        is_new = prev is None
        is_updated = (prev is not None) and (
            notif.update_ms > int(prev.get("update_ms", 0))
            or notif.creation_ms != int(prev.get("creation_ms", 0))
        )
        if not (is_new or is_updated):
            continue

        if notif.pkg in watch_sms_packages:
            if notif.extras.get("android.textLines") is not None and not notif.ticker_text:
                continue

            sender, body = extract_sms_content(notif)
            if body == "(Empty message)":
                continue
            dedup_key = f"{notif.pkg}\n{sender}\n{body}"
            prev_sent = state.recent_sms_hashes.get(dedup_key)
            within_60s = prev_sent is not None and (ts - float(prev_sent.get("ts", 0.0))) <= 60.0
            brand_new_distinct = (
                is_new
                and notif.creation_ms > 0
                and notif.creation_ms == notif.update_ms
                and (
                    prev_sent is None
                    or int(prev_sent.get("creation_ms", 0)) != notif.creation_ms
                )
            )
            if within_60s and not brand_new_distinct:
                continue

            state.recent_sms_hashes[dedup_key] = {
                "ts": ts,
                "creation_ms": notif.creation_ms,
            }
            alerts.append(
                AlertEvent(
                    title="💬 New SMS",
                    description=body,
                    color=COLOR_BLUE,
                    fields=[
                        {"name": "From", "value": sender, "inline": True},
                        {"name": "Package", "value": notif.pkg, "inline": True},
                        _sim_summary_field(telephony),
                    ],
                )
            )
        elif notif.pkg in CALL_MISSED_PACKAGES:
            if (ts - state.last_missed_call_ts) <= 30.0:
                continue
            title = (notif.extras.get("android.title") or "Missed call").strip()
            detail = (
                notif.extras.get("android.bigText")
                or notif.extras.get("android.text")
                or notif.ticker_text
                or "Missed call notification on tablet"
            ).strip()
            state.last_missed_call_ts = ts
            alerts.append(
                AlertEvent(
                    title="📵 Missed Call",
                    description=f"**{title}** — {detail}",
                    color=COLOR_RED,
                    fields=[
                        {"name": "Source", "value": notif.pkg, "inline": True},
                        _sim_summary_field(telephony),
                    ],
                )
            )

    state.seen_notifs = current_seen
    return alerts


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def send_discord_embed(
    alert: AlertEvent, config: Config, *, raise_on_error: bool = False
) -> bool:
    """Post a rich Discord embed via webhook with Cloudflare-safe User-Agent & retries."""
    if not config.discord_webhook_url:
        fields_txt = ", ".join(f"{f['name']}={f['value']}" for f in alert.fields)
        print(
            f"[DRY-RUN: DISCORD_WEBHOOK_URL not set] {alert.title} | {alert.description} | {fields_txt}",
            flush=True,
        )
        return False

    embed_fields = [
        {
            "name": _truncate(str(f["name"]), 256),
            "value": _truncate(str(f["value"]) or "-", 1000),
            "inline": bool(f.get("inline", False)),
        }
        for f in alert.fields[:25]
    ]
    serial_label = config.adb_serial or "auto-usb"
    payload: dict[str, object] = {
        "embeds": [
            {
                "title": _truncate(alert.title, 256),
                "description": _truncate(alert.description, 4000),
                "color": alert.color,
                "fields": embed_fields,
                "footer": {"text": f"Huawei BG2-U01 ({serial_label})"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }
    if config.discord_mention:
        payload["content"] = config.discord_mention

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "SIM-ADB-Notifier/1.0",
    }

    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            config.discord_webhook_url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
        except urllib.error.HTTPError as exc:
            last_err = exc
            err_body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < 2:
                retry_after = exc.headers.get("Retry-After")
                wait_s = 2.0
                if retry_after:
                    try:
                        wait_s = float(retry_after)
                    except ValueError:
                        wait_s = 2.0
                else:
                    try:
                        wait_s = float(json.loads(err_body).get("retry_after", 2.0))
                    except (ValueError, TypeError):
                        wait_s = 2.0
                time.sleep(min(max(wait_s, 0.5), 15.0))
                continue
            if 500 <= exc.code < 600 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            msg = f"Discord webhook HTTP {exc.code}: {err_body[:300]}"
            print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
            if raise_on_error:
                raise RuntimeError(msg) from exc
            return False
        except urllib.error.URLError as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue

    msg = f"Discord webhook request failed: {last_err}"
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
    if raise_on_error:
        raise RuntimeError(msg) from last_err
    return False


def _service_active_status() -> str:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = (proc.stdout or proc.stderr or "unknown").strip()
        return out or "unknown"
    except OSError as exc:
        return f"unavailable ({exc})"


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = NotifierState.load(args.state)
    svc_status = _service_active_status()
    webhook_set = bool(config.discord_webhook_url)

    print(f"Service ({SERVICE_NAME}): {svc_status}")
    print(f"Config path: {args.config}")
    print(
        f"Discord webhook: {'configured' if webhook_set else 'NOT CONFIGURED (dry-run only)'}"
    )
    print(
        f"State file ({args.state}): {'initialized' if state.initialized else 'not initialized'}"
    )
    print(f"Target ADB serial: {config.adb_serial or '(auto USB)'}")

    try:
        telephony_raw, notif_raw = run_adb_snapshot(config.adb_serial)
    except AdbTransportError as exc:
        print(f"ADB status: DISCONNECTED ({exc})", file=sys.stderr)
        return 1

    telephony = parse_telephony(telephony_raw)
    notifs = parse_notifications(notif_raw)
    call_name = CALL_STATE_NAMES.get(
        int(telephony["call_state"]), str(telephony["call_state"])
    )
    sig = telephony["signal_dbm"]
    sig_str = f"{sig} dBm" if sig is not None else "n/a"

    print("ADB status: CONNECTED")
    print(f"SIM carrier: {telephony['carrier']} ({telephony['voice_state']})")
    print(f"Signal strength: {sig_str}")
    print(f"Call state: {call_name} (incoming={telephony['incoming_number'] or 'none'})")
    print(f"Active notifications ({len(notifs)}):")
    for notif in notifs:
        title = notif.extras.get("android.title") or "-"
        ongoing = " [ongoing]" if (notif.flags & ONGOING_FLAGS_MASK) else ""
        print(f"  - {notif.pkg}{ongoing}: {title}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    fields: list[dict[str, object]] = []
    try:
        telephony_raw, _ = run_adb_snapshot(config.adb_serial)
        telephony = parse_telephony(telephony_raw)
        call_name = CALL_STATE_NAMES.get(
            int(telephony["call_state"]), str(telephony["call_state"])
        )
        fields.append(_sim_summary_field(telephony))
        fields.append({"name": "Call State", "value": call_name, "inline": True})
        fields.append({"name": "ADB Link", "value": "Connected", "inline": True})
    except AdbTransportError as exc:
        fields.append({"name": "ADB Link", "value": f"Offline ({exc})", "inline": False})

    alert = AlertEvent(
        title="🧪 Test Alert",
        description="USB ADB SIM Call & SMS Notifier test message.",
        color=COLOR_PURPLE,
        fields=fields,
    )
    if not config.discord_webhook_url:
        send_discord_embed(alert, config)
        print(
            "Configure a webhook first with:\n"
            f"  {sys.argv[0]} set-webhook 'https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN' --test",
            file=sys.stderr,
        )
        return 1

    try:
        send_discord_embed(alert, config, raise_on_error=True)
    except RuntimeError as exc:
        print(f"Failed to send test alert: {exc}", file=sys.stderr)
        return 1

    print("Test alert sent to Discord successfully.")
    return 0


def cmd_set_webhook(args: argparse.Namespace) -> int:
    url = args.url.strip()
    if not DISCORD_WEBHOOK_RE.match(url):
        print(
            "Invalid Discord webhook URL. Expected format: "
            "https://discord.com/api/webhooks/<id>/<token>",
            file=sys.stderr,
        )
        return 1

    updates = {"DISCORD_WEBHOOK_URL": url}
    if args.mention is not None:
        updates["DISCORD_MENTION"] = args.mention.strip()

    update_config_file(args.config, updates, chmod_600=True)
    print(f"Updated {args.config} (permissions set to 600).")

    if _service_active_status() == "active":
        proc = subprocess.run(
            ["systemctl", "--user", "restart", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            print(f"Restarted {SERVICE_NAME}.")
        else:
            print(
                f"Warning: failed to restart {SERVICE_NAME}: {proc.stderr.strip()}",
                file=sys.stderr,
            )

    if args.test:
        return cmd_test(args)
    return 0


def cmd_self_test(_args: argparse.Namespace) -> int:
    """Verify parsers and state machine with realistic Huawei Android 7.0 fixtures."""
    telephony_idle = """\
last known state:
  Phone Id=0
  mCallState=0
  mCallIncomingNumber=
  mServiceState=0 0 voice home data home cirkle cirkle 47002 cirkle cirkle 47002  EDGE EDGE CSS not supported -1 -1 RoamInd=-1 DefRoamInd=-1 EmergOnly=false IsDataRoamingFromRegistration=false
  mSignalStrength=SignalStrength: -71 0 -1 -1 -1 -1 -1 -1 -1 99 2147483647 2147483647 2147483647 2147483647 2147483647 false gw|lte
"""
    telephony_ringing = telephony_idle.replace("mCallState=0", "mCallState=1").replace(
        "mCallIncomingNumber=", "mCallIncomingNumber=+8801712345678"
    )

    notif_baseline = """\
Current Notification Manager state:
  Notification List:
    NotificationRecord(0x0c86d42d: pkg=com.thanksmister.iot.wallpanel user=UserHandle{0} id=1 tag=null importance=2 key=0|com.thanksmister.iot.wallpanel|1|null|10092: Notification(pri=0 flags=0x162))
      key=0|com.thanksmister.iot.wallpanel|1|null|10092
      tickerText=null
      contentView=null
      defaults=0x00000000 flags=0x00000162
      extras={
        android.title=String (WallPanel)
        android.text=String (WallPanel Running…)
      }
      mCreationTimeMs=1000
      mUpdateTimeMs=1000
  mArchive=Archive (1 notifications)
    StatusBarNotification(pkg=com.android.mms user=UserHandle{0} id=999)
"""

    notif_single_sms = """\
Current Notification Manager state:
  Notification List:
    NotificationRecord(0x11111111: pkg=com.android.mms user=UserHandle{0} id=123 tag=null importance=4 key=0|com.android.mms|123|null|10010: Notification(pri=1 flags=0x10))
      key=0|com.android.mms|123|null|10010
      tickerText=+8801XXXXXXXXX: Your OTP is 482910
Do not share it.
      contentView=null
      defaults=0x00000001 flags=0x00000010
      extras={
        android.title=String (+8801XXXXXXXXX)
        android.text=String (Your OTP is 482910...)
        android.bigText=String (Your OTP is 482910
Do not share it.)
        android.textLines=null
      }
      mCreationTimeMs=2000
      mUpdateTimeMs=2000
  mArchive=Archive (0 notifications)
"""

    notif_grouped_sms = """\
Current Notification Manager state:
  Notification List:
    NotificationRecord(0x11111111: pkg=com.android.mms user=UserHandle{0} id=123 tag=null importance=4 key=0|com.android.mms|123|null|10010: Notification(pri=1 flags=0x10))
      key=0|com.android.mms|123|null|10010
      tickerText=BankAlert: BDT 5,000 credited
      contentView=null
      defaults=0x00000001 flags=0x00000010
      extras={
        android.title=String (2 new messages)
        android.text=String (2 new messages)
        android.textLines=CharSequence[] (2)
      }
      mCreationTimeMs=2000
      mUpdateTimeMs=3000
  mArchive=Archive (0 notifications)
"""

    notif_grouped_dismiss = """\
Current Notification Manager state:
  Notification List:
    NotificationRecord(0x11111111: pkg=com.android.mms user=UserHandle{0} id=123 tag=null importance=4 key=0|com.android.mms|123|null|10010: Notification(pri=1 flags=0x10))
      key=0|com.android.mms|123|null|10010
      tickerText=null
      contentView=null
      defaults=0x00000001 flags=0x00000010
      extras={
        android.title=String (2 new messages)
        android.text=String (2 new messages)
        android.textLines=CharSequence[] (2)
      }
      mCreationTimeMs=2000
      mUpdateTimeMs=4000
  mArchive=Archive (0 notifications)
"""

    state = NotifierState()
    t_idle = parse_telephony(telephony_idle)
    assert t_idle["carrier"] == "cirkle", t_idle
    assert t_idle["signal_dbm"] == -71, t_idle
    assert t_idle["call_state"] == 0, t_idle

    # Baseline first run must emit nothing
    alerts = process_snapshot(
        state, t_idle, parse_notifications(notif_baseline), DEFAULT_SMS_PACKAGES, now_ts=10.0
    )
    assert alerts == [], f"expected no baseline alerts, got {alerts}"

    # (a) Incoming ringing call -> Missed call
    t_ring = parse_telephony(telephony_ringing)
    alerts = process_snapshot(state, t_ring, [], DEFAULT_SMS_PACKAGES, now_ts=20.0)
    assert len(alerts) == 1 and alerts[0].title == "📞 Incoming Call", alerts
    assert "+8801712345678" in alerts[0].description, alerts

    alerts = process_snapshot(state, t_idle, [], DEFAULT_SMS_PACKAGES, now_ts=32.5)
    assert len(alerts) == 1 and alerts[0].title == "📵 Missed Call", alerts
    assert "+8801712345678" in alerts[0].description, alerts
    assert any(f["value"] == "12.5s" for f in alerts[0].fields), alerts[0].fields

    # (b) Single multiline OTP SMS via bigText
    alerts = process_snapshot(
        state, t_idle, parse_notifications(notif_single_sms), DEFAULT_SMS_PACKAGES, now_ts=40.0
    )
    assert len(alerts) == 1 and alerts[0].title == "💬 New SMS", alerts
    assert alerts[0].description == "Your OTP is 482910\nDo not share it.", alerts[0]
    assert alerts[0].fields[0]["value"] == "+8801XXXXXXXXX", alerts[0].fields

    # (c) 2nd unread SMS grouped under InboxStyle extracted from tickerText
    alerts = process_snapshot(
        state, t_idle, parse_notifications(notif_grouped_sms), DEFAULT_SMS_PACKAGES, now_ts=50.0
    )
    assert len(alerts) == 1 and alerts[0].title == "💬 New SMS", alerts
    assert alerts[0].fields[0]["value"] == "BankAlert", alerts[0].fields
    assert alerts[0].description == "BDT 5,000 credited", alerts[0]

    # (d) Dismissal / non-new grouped update with tickerText=null ignored
    alerts = process_snapshot(
        state,
        t_idle,
        parse_notifications(notif_grouped_dismiss),
        DEFAULT_SMS_PACKAGES,
        now_ts=60.0,
    )
    assert alerts == [], f"expected dismissal update to be ignored, got {alerts}"

    print("SELF-TEST OK")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    state_path: Path = args.state

    config = load_config(config_path)
    config_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
    state = NotifierState.load(state_path)

    stop_event = threading.Event()
    wake_event = threading.Event()

    def _handle_sig(_signum: int, _frame: object) -> None:
        stop_event.set()
        wake_event.set()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    watcher = EventsWakeThread(config.adb_serial, wake_event, stop_event)
    watcher.start()

    print(
        f"Starting sim-adb-notifier (serial={config.adb_serial or 'auto'}, "
        f"poll={config.poll_interval_seconds}s, "
        f"webhook={'configured' if config.discord_webhook_url else 'dry-run'})",
        flush=True,
    )

    disconnect_started_ts: float | None = None
    disconnect_alert_sent = False
    last_telephony: dict[str, object] = {}

    try:
        while not stop_event.is_set():
            wake_event.clear()

            if config_path.exists():
                cur_mtime = config_path.stat().st_mtime
                if cur_mtime != config_mtime:
                    config_mtime = cur_mtime
                    new_cfg = load_config(config_path)
                    serial_changed = new_cfg.adb_serial != config.adb_serial
                    config = new_cfg
                    print("Reloaded config.env.", flush=True)
                    if serial_changed:
                        watcher.terminate_proc()
                        watcher = EventsWakeThread(
                            config.adb_serial, wake_event, stop_event
                        )
                        watcher.start()

            watch_sms = DEFAULT_SMS_PACKAGES | config.extra_watch_packages

            try:
                telephony_raw, notif_raw = run_adb_snapshot(config.adb_serial)
                telephony = parse_telephony(telephony_raw)
                notifs = parse_notifications(notif_raw)

                if (
                    int(telephony["call_state"]) == 1
                    and state.prev_call_state != 1
                    and not _resolve_caller_from_snapshot(telephony, notifs)
                ):
                    time.sleep(0.6)
                    telephony_raw, notif_raw = run_adb_snapshot(config.adb_serial)
                    telephony = parse_telephony(telephony_raw)
                    notifs = parse_notifications(notif_raw)

                if disconnect_started_ts is not None:
                    downtime = time.time() - disconnect_started_ts
                    print(f"ADB reconnected after {downtime:.1f}s.", flush=True)
                    if disconnect_alert_sent:
                        send_discord_embed(
                            AlertEvent(
                                title="✅ SIM Tablet Reconnected",
                                description=f"USB ADB link restored after **{downtime:.0f}s**.",
                                color=COLOR_AMBER,
                                fields=[_sim_summary_field(telephony)],
                            ),
                            config,
                        )
                    disconnect_started_ts = None
                    disconnect_alert_sent = False

                if not last_telephony:
                    print(
                        f"Connected to {config.adb_serial or 'USB device'}: "
                        f"carrier={telephony['carrier']} ({telephony['voice_state']}), "
                        f"signal={telephony['signal_dbm']} dBm, "
                        f"baseline_initialized={state.initialized}",
                        flush=True,
                    )
                last_telephony = telephony

                was_initialized = state.initialized
                alerts = process_snapshot(state, telephony, notifs, watch_sms)
                state.save(state_path)
                if not was_initialized and state.initialized:
                    print(
                        f"Baselined {len(state.seen_notifs)} active notifications into {state_path.name}.",
                        flush=True,
                    )

                for alert in alerts:
                    print(f"Alert: {alert.title} - {alert.description}", flush=True)
                    send_discord_embed(alert, config)

            except AdbTransportError as exc:
                now = time.time()
                if disconnect_started_ts is None:
                    disconnect_started_ts = now
                    print(f"ADB disconnected: {exc}", file=sys.stderr, flush=True)
                elif (
                    config.alert_on_disconnect
                    and not disconnect_alert_sent
                    and (now - disconnect_started_ts) >= 120.0
                ):
                    disconnect_alert_sent = True
                    send_discord_embed(
                        AlertEvent(
                            title="⚠️ SIM Tablet Disconnected",
                            description=f"USB ADB unreachable for >120s (`{exc}`).",
                            color=COLOR_AMBER,
                        ),
                        config,
                    )

            wake_event.wait(config.poll_interval_seconds)
    finally:
        stop_event.set()
        watcher.terminate_proc()

    print("Stopped sim-adb-notifier cleanly.", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="USB ADB SIM Call & SMS Discord Notifier"
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config.env"
    )
    parser.add_argument(
        "--state", type=Path, default=DEFAULT_STATE_PATH, help="Path to state.json"
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Run the persistent notifier daemon (default)")
    sub.add_parser("status", help="Show service, USB ADB, and SIM status")
    sub.add_parser("test", help="Send a test alert to the configured Discord webhook")
    sub.add_parser("self-test", help="Run built-in parser and state machine tests")

    sw = sub.add_parser("set-webhook", help="Save Discord webhook URL in config.env")
    sw.add_argument("url", help="Discord webhook URL")
    sw.add_argument("--mention", default=None, help="Optional DISCORD_MENTION value")
    sw.add_argument(
        "--test", action="store_true", help="Send a test alert after saving"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.command or "run"
    if cmd == "run":
        return cmd_run(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "test":
        return cmd_test(args)
    if cmd == "set-webhook":
        return cmd_set_webhook(args)
    if cmd == "self-test":
        return cmd_self_test(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
