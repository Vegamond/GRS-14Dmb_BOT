#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import argparse
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from zoneinfo import ZoneInfo

import requests

KYIV_TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"

# ----------------------------
# LINK FIX: strict URL regex (excludes Cyrillic, spaces, etc.)
# Prevents "…ідентифікатор" from sticking to URL.
# ----------------------------
URL_RE = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE
)

# ----------------------------
# Pair numbering by start time
# ----------------------------
PAIR_BY_START = {
    "09:00": 1,
    "10:40": 2,
    "12:30": 3,
    "14:10": 4,
    "15:40": 5,
}


def pair_no(t: dt.datetime) -> Optional[int]:
    s = t.astimezone(KYIV_TZ).strftime("%H:%M")
    return PAIR_BY_START.get(s)


# ----------------------------
# Moodle links per discipline
# ----------------------------
MOODLE_LINKS = {
    "Безпека життєдіяльності та охорона праці": "https://distance.kuk.edu.ua/mod/attendance/view.php?id=179093",
    "Основи правознавства": "https://distance.kuk.edu.ua/mod/attendance/view.php?id=179091",
    "Устаткування готельно-ресторанних комплексів": "https://distance.kuk.edu.ua/mod/attendance/view.php?id=178888",
    "Маркетинг, реклама та PR готельно-ресторанного і туристичного бізнесу": "https://distance.kuk.edu.ua/course/view.php?id=8577",
    "Енологія і еногастрономія": "https://distance.kuk.edu.ua/course/view.php?id=8576",
    "Ресторанне обслуговування: організація і технології": "https://distance.kuk.edu.ua/mod/attendance/view.php?id=147491",
    "Готельне обслуговування: організація і технології": "https://distance.kuk.edu.ua/course/view.php?id=7724",
    "Економіка підприємства": "https://distance.kuk.edu.ua/course/view.php?id=5778",
    "Психологія": "https://distance.kuk.edu.ua/mod/attendance/view.php?id=179013",
    "Документаційне забезпечення управління": "https://distance.kuk.edu.ua/mod/attendance/view.php?id=179019",
}


def normalize_discipline(name: str) -> str:
    return " ".join((name or "").replace("’", "'").split()).casefold()


MOODLE_LINKS_NORM = {
    normalize_discipline(k): v
    for k, v in MOODLE_LINKS.items()
    if v
}


# ----------------------------
# iCal unescape (RFC5545)
# ----------------------------
def ics_unescape(s: str) -> str:
    if not s:
        return ""
    return (s
            .replace(r"\n", "\n")
            .replace(r"\N", "\n")
            .replace(r"\,", ",")
            .replace(r"\;", ";")
            .replace(r"\\", "\\"))


# ----------------------------
# Models
# ----------------------------
@dataclass
class Event:
    start: dt.datetime
    end: dt.datetime
    summary: str
    description: str
    location: str


# ----------------------------
# Utils
# ----------------------------
def now_kyiv() -> dt.datetime:
    return dt.datetime.now(tz=KYIV_TZ)


def iso_date(d: dt.date) -> str:
    return d.isoformat()


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_post(state: Dict, key: str, stamp: str) -> bool:
    last = state.get(key)
    return last != stamp


def mark_posted(state: Dict, key: str, stamp: str) -> None:
    state[key] = stamp


def env_required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def env_optional_int(name: str) -> Optional[int]:
    v = os.getenv(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


# ----------------------------
# Posting policy by weekday
# Monday=0 ... Sunday=6
# ----------------------------
def is_saturday(day: dt.date) -> bool:
    return day.weekday() == 5


def is_sunday(day: dt.date) -> bool:
    return day.weekday() == 6


# ----------------------------
# ICS parsing
# ----------------------------
def fetch_ics(url: str, timeout_s: int = 30) -> str:
    resp = requests.get(url, timeout=timeout_s)
    resp.raise_for_status()
    return resp.text


def _unfold_ics_lines(ics_text: str) -> List[str]:
    raw = ics_text.splitlines()
    out = []
    for line in raw:
        if not line:
            out.append(line)
            continue
        if line.startswith(" ") or line.startswith("\t"):
            if out:
                out[-1] += line[1:]
            else:
                out.append(line.lstrip())
        else:
            out.append(line)
    return out


def _parse_dt(value: str, tzid: Optional[str]) -> dt.datetime:
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        d = dt.datetime.strptime(value, "%Y%m%d").date()
        return dt.datetime(
            d.year, d.month, d.day, 0, 0,
            tzinfo=ZoneInfo(tzid) if tzid else KYIV_TZ
        )

    if value.endswith("Z"):
        base = dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        return base.astimezone(KYIV_TZ)

    naive = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    tz = ZoneInfo(tzid) if tzid else KYIV_TZ
    return naive.replace(tzinfo=tz).astimezone(KYIV_TZ)


def parse_ics_events(ics_text: str) -> List[Event]:
    lines = _unfold_ics_lines(ics_text)
    events: List[Event] = []

    in_event = False
    cur: Dict[str, Tuple[Optional[str], str]] = {}

    def flush():
        nonlocal cur
        if not cur:
            return

        dtstart_tz, dtstart_val = cur.get("DTSTART", (None, ""))
        dtend_tz, dtend_val = cur.get("DTEND", (None, ""))
        summary_raw = cur.get("SUMMARY", (None, ""))[1]
        description_raw = cur.get("DESCRIPTION", (None, ""))[1]
        location_raw = cur.get("LOCATION", (None, ""))[1]

        if not dtstart_val or not dtend_val:
            cur = {}
            return

        start = _parse_dt(dtstart_val, dtstart_tz)
        end = _parse_dt(dtend_val, dtend_tz)

        summary = ics_unescape(summary_raw).strip()
        description = ics_unescape(description_raw).strip()
        location = ics_unescape(location_raw).strip()

        events.append(Event(
            start=start,
            end=end,
            summary=summary,
            description=description,
            location=location,
        ))
        cur = {}

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
            continue
        if line == "END:VEVENT":
            if in_event:
                flush()
            in_event = False
            continue
        if not in_event:
            continue

        if ":" not in line:
            continue

        left, value = line.split(":", 1)
        key = left
        tzid = None

        if ";" in left:
            key, params = left.split(";", 1)
            m = re.search(r"TZID=([^;]+)", params)
            if m:
                tzid = m.group(1)

        key = key.strip().upper()
        value = value.strip()

        if key in {"DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "LOCATION"}:
            cur[key] = (tzid, value)

    events.sort(key=lambda e: e.start)
    return events


def events_in_range(events: List[Event], start_date: dt.date, end_date: dt.date) -> List[Event]:
    out = []
    for ev in events:
        d = ev.start.astimezone(KYIV_TZ).date()
        if start_date <= d <= end_date:
            out.append(ev)
    return out


# ----------------------------
# Extractors
# ----------------------------
UA_DOW = {
    0: "Понеділок",
    1: "Вівторок",
    2: "Середа",
    3: "Четвер",
    4: "Пʼятниця",
    5: "Субота",
    6: "Неділя",
}


def detect_type(tail: str) -> Optional[str]:
    t = tail.strip().lower()
    if "лекц" in t:
        return "Лекція"
    if "практ" in t or t == "пр." or t == "пр":
        return "Практичне"
    if "лаб" in t:
        return "Лабораторна"
    if "семінар" in t:
        return "Семінар"
    return None


def split_summary(summary: str) -> Tuple[str, Optional[str]]:
    s = summary.strip()

    if "—" in s:
        left, right = s.rsplit("—", 1)
        etype = detect_type(right)
        if etype:
            return left.strip(), etype

    if " - " in s:
        left, right = s.rsplit(" - ", 1)
        etype = detect_type(right)
        if etype:
            return left.strip(), etype

    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", s)
    if m:
        base = m.group(1).strip()
        tail = m.group(2).strip()
        etype = detect_type(tail)
        if etype:
            return base, etype

    return s, None


def _normalize_for_links(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\\n", "\n")
    t = t.replace("\u200b", "")
    return t


def extract_zoom_links(text: str) -> List[str]:
    t = _normalize_for_links(text)
    links = URL_RE.findall(t)
    zoom = [l for l in links if "zoom.us" in l.lower()]
    rest = [l for l in links if l not in zoom]
    return zoom + rest


def extract_teacher(description: str) -> Optional[str]:
    if not description:
        return None

    lines = [l.strip() for l in description.splitlines() if l.strip()]
    patterns = [
        r"^(?:доц\.?|доцент)\s*[:\-]?\s*(.+)$",
        r"^(?:викл\.?|викладач)\s*[:\-]?\s*(.+)$",
        r"^(?:проф\.?|професор)\s*[:\-]?\s*(.+)$",
        r"^(?:асист\.?|асистент)\s*[:\-]?\s*(.+)$",
        r"^(?:Доц\.?|Доцент)\s*[:\-]?\s*(.+)$",
        r"^(?:Викл\.?|Викладач)\s*[:\-]?\s*(.+)$",
        r"^(?:Проф\.?|Професор)\s*[:\-]?\s*(.+)$",
    ]

    for line in lines:
        for pat in patterns:
            m = re.match(pat, line, flags=re.IGNORECASE)
            if m:
                return m.group(1).strip()
    return None


def extract_passcode(text: str) -> Optional[str]:
    if not text:
        return None

    t = text.replace("\\n", "\n")
    patterns = [
        r"(?:Код\s*доступу|Код\s*доступа|Passcode|Пароль)\s*[:=\-]\s*([^\s,;]+)",
        r"(?:^|\n)\s*Код\s*[:=\-]\s*([^\s,;]+)",
    ]

    for pat in patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def classify_place(location: str, description: str) -> str:
    blob = f"{location}\n{description}".lower()

    if "online" in blob or "zoom" in blob:
        m = re.search(r"(ауд\.?\s*\d+)", blob, flags=re.IGNORECASE)
        if m:
            return f"🌐 Online (Zoom) • 🏫 {m.group(1).replace('ауд', 'ауд.').strip()}"
        return "🌐 Online (Zoom)"

    m2 = re.search(r"(ауд\.?\s*\d+)", blob, flags=re.IGNORECASE)
    if m2:
        return f"🏫 {m2.group(1).replace('ауд', 'ауд.').strip()}"

    if location.strip():
        return f"📍 {location.strip()}"

    return "📍 (місце не вказано)"


# ----------------------------
# Weather (Dnipro) via Open-Meteo
# ----------------------------
def get_weather_dnipro(day: dt.date) -> Optional[Dict]:
    lat, lon = 48.45, 34.98
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Europe%2FKyiv"
    )
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        dates = data.get("daily", {}).get("time", [])
        if not dates:
            return None
        if day.isoformat() not in dates:
            return None
        idx = dates.index(day.isoformat())
        wcode = data["daily"]["weathercode"][idx]
        tmax = data["daily"]["temperature_2m_max"][idx]
        tmin = data["daily"]["temperature_2m_min"][idx]
        p = data["daily"]["precipitation_probability_max"][idx]
        return {
            "desc": weathercode_ua(wcode),
            "tmin": int(round(tmin)),
            "tmax": int(round(tmax)),
            "p": int(p) if p is not None else None,
        }
    except Exception:
        return None


def weathercode_ua(code: int) -> str:
    mapping = {
        0: "ясно",
        1: "переважно ясно",
        2: "мінлива хмарність",
        3: "хмарно",
        45: "туман",
        48: "паморозь / туман",
        51: "мряка",
        53: "мряка",
        55: "мряка",
        61: "дощ",
        63: "дощ",
        65: "сильний дощ",
        66: "крижаний дощ",
        67: "крижаний дощ",
        71: "сніг",
        73: "сніг",
        75: "сильний сніг",
        77: "снігова крупа",
        80: "зливи",
        81: "зливи",
        82: "сильні зливи",
        85: "снігопад",
        86: "сильний снігопад",
        95: "гроза",
        96: "гроза з градом",
        99: "гроза з градом",
    }
    return mapping.get(code, f"погода (код: {code})")


def format_weather_block(day: dt.date, label: str) -> str:
    w = get_weather_dnipro(day)
    if not w:
        return ""

    lines = [
        f"⛅ Погода в Дніпрі на {label}:",
        f"• {w['desc']}",
        f"• 🌡️ Мін/Макс: {w['tmin']}°C / {w['tmax']}°C",
    ]
    if w.get("p") is not None:
        lines.append(f"• ☔ Ймовірність опадів: {w['p']}%")

    return "\n".join(lines) + "\n\n"


# ----------------------------
# Formatting
# ----------------------------
def hhmm(t: dt.datetime) -> str:
    return t.astimezone(KYIV_TZ).strftime("%H:%M")


def fmt_date_short(d: dt.date) -> str:
    return d.strftime("%d.%m")


def day_header(d: dt.date) -> str:
    dow = UA_DOW[d.weekday()]
    return f"📅 <b>{dow}</b> • <b>{fmt_date_short(d)}</b>"


def separator() -> str:
    return "━━━━━━━━━━━━━━━━━━━━"


def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_html_attr(s: str) -> str:
    return escape_html(s).replace('"', "&quot;")


def format_day(events: List[Event], day: dt.date) -> str:
    lines = []
    lines.append(day_header(day))
    lines.append("")

    if not events:
        lines.append("— (пар немає)")
        return "\n".join(lines)

    for ev in events:
        discipline, etype = split_summary(ev.summary)
        teacher = extract_teacher(ev.description)
        passcode = extract_passcode(ev.description + "\n" + ev.location + "\n" + ev.summary)
        place = classify_place(ev.location, ev.description)

        links = extract_zoom_links(ev.description + "\n" + ev.location)
        link = links[0] if links else None

        moodle_url = MOODLE_LINKS_NORM.get(normalize_discipline(discipline))

        pno = pair_no(ev.start)
        pfx = f"{pno} пара " if pno else ""

        lines.append(f"🕒 <b>{pfx}{hhmm(ev.start)}–{hhmm(ev.end)}</b>")
        lines.append(f"📚 <b>{escape_html(discipline)}</b>")

        if moodle_url:
            href_m = escape_html_attr(moodle_url)
            lines.append(f'📘 <a href="{href_m}">Відкрити Moodle</a>')

        if etype:
            lines.append(f"🎓 {etype}")

        if teacher:
            lines.append(f"👩‍🏫 {escape_html(teacher)}")

        lines.append(escape_html(place))

        if link:
            href = escape_html_attr(link)
            lines.append(f'🔗 <a href="{href}">Відкрити Zoom</a>')

        if passcode:
            lines.append("🔑 Код доступу:")
            lines.append(f"📎 <code>{escape_html(passcode)}</code>")

        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def format_week_message(events: List[Event], start_day: dt.date, end_day: dt.date) -> str:
    header = (
        f"🗓️ <b>Розклад на тиждень</b>\n"
        f"<b>{fmt_date_short(start_day)} – {fmt_date_short(end_day)}</b>\n\n"
    )

    by_day: Dict[dt.date, List[Event]] = {
        start_day + dt.timedelta(days=i): []
        for i in range((end_day - start_day).days + 1)
    }

    for ev in events:
        by_day[ev.start.astimezone(KYIV_TZ).date()].append(ev)

    blocks = []
    for d in by_day.keys():
        blocks.append(separator())
        blocks.append(format_day(by_day[d], d))
    blocks.append(separator())

    return header + "\n".join(blocks) + f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"


# ----------------------------
# Telegram
# ----------------------------
def tg_send_message(
    token: str,
    chat_id: str,
    text: str,
    message_thread_id: Optional[int] = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id

    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")


# ----------------------------
# Main
# ----------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["today", "tomorrow", "week"], help="Posting mode")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force post even if weekday restrictions would normally skip it"
    )
    args = parser.parse_args()

    token = env_required("TG_BOT_TOKEN")
    chat_id = env_required("TG_CHAT_ID")
    ics_url = env_required("GCAL_ICS_URL")

    schedule_thread_id = env_optional_int("TG_SCHEDULE_THREAD_ID")

    state = load_state()

    ics = fetch_ics(ics_url)
    all_events = parse_ics_events(ics)

    today = now_kyiv().date()

    if args.mode == "today":
        if is_saturday(today) or is_sunday(today):
            print("Skipping 'today': no today-posts on Saturday or Sunday.")
            return

        target = today
        stamp = f"today:{iso_date(target)}"
        if not should_post(state, "last_today", stamp):
            print("Already posted today schedule for this date. Exiting.")
            return

        day_events = events_in_range(all_events, target, target)

        weather = format_weather_block(target, "сьогодні")
        msg = "<b>Доброго ранку шановні студенти!</b> ☀️\n\n" + weather
        msg += f"🗓️ <b>Розклад на сьогодні ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"

        tg_send_message(token, chat_id, msg, message_thread_id=None)

        mark_posted(state, "last_today", stamp)
        save_state(state)
        print("Posted today schedule.")

    elif args.mode == "tomorrow":
        if is_saturday(today):
            print("Skipping 'tomorrow': no posts on Saturday.")
            return

        target = today + dt.timedelta(days=1)
        stamp = f"tomorrow:{iso_date(target)}"
        if not should_post(state, "last_tomorrow", stamp):
            print("Already posted tomorrow schedule for this date. Exiting.")
            return

        day_events = events_in_range(all_events, target, target)

        weather = format_weather_block(target, "завтра")
        msg = "<b>Добрий вечір шановні студенти!</b> 🌙\n\n" + weather
        msg += f"🗓️ <b>Розклад на завтра ({fmt_date_short(target)})</b>\n\n"
        msg += format_day(day_events, target)
        msg += f"\n\n⏱️ Оновлено: {now_kyiv().strftime('%H:%M')}"

        tg_send_message(token, chat_id, msg, message_thread_id=None)

        mark_posted(state, "last_tomorrow", stamp)
        save_state(state)
        print("Posted tomorrow schedule.")

    elif args.mode == "week":
        if not args.force and not is_sunday(today):
            print("Skipping 'week': weekly post should run on Sunday only.")
            return

        this_monday = today - dt.timedelta(days=today.weekday())
        next_monday = this_monday + dt.timedelta(days=7)
        next_sunday = next_monday + dt.timedelta(days=6)

        stamp = f"week:{iso_date(next_monday)}:{iso_date(next_sunday)}"
        if not should_post(state, "last_week", stamp):
            print("Already posted weekly schedule for this week-range. Exiting.")
            return

        week_events = events_in_range(all_events, next_monday, next_sunday)
        msg = format_week_message(week_events, next_monday, next_sunday)

        if schedule_thread_id is None:
            print("WARNING: TG_SCHEDULE_THREAD_ID not set. Weekly post will go to general chat.")

        tg_send_message(token, chat_id, msg, message_thread_id=schedule_thread_id)

        mark_posted(state, "last_week", stamp)
        save_state(state)
        print("Posted weekly schedule (next week).")


if __name__ == "__main__":
    main()
