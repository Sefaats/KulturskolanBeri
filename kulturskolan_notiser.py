#!/usr/bin/env python3
"""
Bevakar Kulturskolan Stockholms kurskatalog och skickar notiser via ntfy.

Bevakar: kurser på Kungsholmen / Kungsholmens kulturhus för barn födda 2020
(ålder 6-7 under hösten 2026 och våren 2027).

Du får notis när
  1. en NY kurs dyker upp som matchar, eller
  2. en kurs som var full får LEDIGA platser (om katalogen anger platsantal).

TIDER
  - Inga kontroller och inga notiser mellan 20:00 och 08:00 (svensk tid).
    Det som dyker upp på natten meddelas vid första kontrollen efter 08:00.
  - Bevakningen är bara aktiv under perioderna i ACTIVE_PERIODS nedan.

KÖRNING
  Avsett att köras av GitHub Actions (se kulturskolan.yml), men fungerar
  också på en vanlig dator:
    pip install requests playwright && playwright install chromium
    export NTFY_TOPIC="ditt-hemliga-amnesnamn"
    python kulturskolan_notiser.py --test        # testnotis
    python kulturskolan_notiser.py --discover    # visa vad katalogen levererar
    python kulturskolan_notiser.py               # en kontroll (för schema/cron)
    python kulturskolan_notiser.py --loop        # kontrollerar var 10:e minut
    Lägg till --force för att köra utanför tider/perioder (t.ex. vid test).

OBS: Skriptet har skrivits utan att kunna nå Speedadmin från testmiljön.
Kör --discover först. Fungerar inte filtreringen, justera inställningarna nedan.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ----------------------------- INSTÄLLNINGAR -----------------------------
CATALOGUE_URL = "https://sestockholm.speedadmin.dk/play/catalogue/courses"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

LOCATION_KEYWORDS = ["kungsholm"]   # matchar Kungsholmen och Kungsholmens kulturhus
AGES = {6, 7}                       # barn födda 2020 (lägg till 8 från hösten 2027)

TZ = ZoneInfo("Europe/Stockholm")
AWAKE_FROM, AWAKE_TO = 8, 20        # kontroller/notiser bara 08:00-20:00

# Perioder då bevakningen är på, som (start, slut) i formatet "MM-DD".
# Perioder får gå över årsskiftet. Tom lista = alltid på.
# OBS: Detta är min gissning, Kulturskolan publicerar inget fast släppdatum.
ACTIVE_PERIODS = [
    ("11-15", "01-31"),   # inför vårterminen (långa kurser startar i januari)
    ("04-15", "06-15"),   # inför höstterminen
    ("08-01", "09-15"),   # terminsstart och eftersläpp av platser
]

INTERVAL_SECONDS = 600              # bara för --loop. Gå inte under ca 5 min.
STATE_FILE = Path(__file__).with_name("kulturskolan_state.json")
# --------------------------------------------------------------------------

ID_KEYS = ("id", "courseId", "course_id", "offeringId", "courseOfferingId",
           "courseCatalogueOfferingId", "offeringBaseId")
NAME_KEYS = ("name", "title", "courseName", "course_name", "displayName",
             "offeringName", "publicName", "externalName")
SEAT_KEY_RE = re.compile(r"(avail|free|vacan|remain|ledig|spots|seats|places)", re.I)
FULL_KEY_RE = re.compile(r"(is_?full|fully_?booked|full$|soldout|sold_out)", re.I)
LOC_KEY_RE = re.compile(r"(loc|place|venue|room|site|department|area|unit|district|stadsdel|school)", re.I)


# ------------------------------- tider ------------------------------------
def in_active_period(now):
    if not ACTIVE_PERIODS:
        return True
    md = now.strftime("%m-%d")
    for start, end in ACTIVE_PERIODS:
        if start <= end:
            if start <= md <= end:
                return True
        elif md >= start or md <= end:  # perioden går över årsskiftet
            return True
    return False


def in_awake_hours(now):
    return AWAKE_FROM <= now.hour < AWAKE_TO


def should_run(force=False, now=None):
    now = now or datetime.now(TZ)
    if force:
        return True, ""
    if not in_active_period(now):
        return False, "utanför bevakningsperiod"
    if not in_awake_hours(now):
        return False, f"tyst tid ({AWAKE_TO}:00-{AWAKE_FROM:02d}:00)"
    return True, ""


# ------------------------------- ntfy -------------------------------------
def notify(title, message, priority=3, tags=None, click=CATALOGUE_URL):
    if not NTFY_TOPIC:
        sys.exit("Sätt miljövariabeln NTFY_TOPIC först.")
    r = requests.post(
        NTFY_SERVER,
        json={
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message[:3500],
            "priority": priority,  # 1-5, 5 = urgent
            "tags": tags or [],
            "click": click,
        },
        timeout=15,
    )
    r.raise_for_status()


# --------------------------- hämta katalogen -------------------------------
def fetch_payloads():
    """Laddar katalogen i en headless-webbläsare och fångar alla JSON-svar."""
    from playwright.sync_api import sync_playwright

    payloads = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="sv-SE")
        page = ctx.new_page()

        def on_response(resp):
            try:
                if "json" in (resp.headers.get("content-type") or ""):
                    payloads.append((resp.url, resp.json()))
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(CATALOGUE_URL, wait_until="networkidle", timeout=60000)
        for _ in range(15):  # scrolla så att fler kurser laddas
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(700)
        page.wait_for_load_state("networkidle")
        browser.close()
    return payloads


# ----------------------------- tolka data ----------------------------------
def first_key(d, keys):
    """Hämtar första befintliga nyckeln, oavsett stora/små bokstäver."""
    lower = {k.lower(): v for k, v in d.items() if isinstance(k, str)}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, "", []):
            return v
    return None


def is_course(d):
    name = first_key(d, NAME_KEYS)
    return isinstance(name, str) and first_key(d, ID_KEYS) is not None


def outermost_courses(obj, out):
    """Hittar yttersta dicts med id + namn (kurser), utan att gå in i dem."""
    if isinstance(obj, dict):
        if is_course(obj):
            out.append(obj)
            return
        for v in obj.values():
            outermost_courses(v, out)
    elif isinstance(obj, list):
        for v in obj:
            outermost_courses(v, out)


def all_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from all_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_dicts(v)


def dump(d):
    return json.dumps(d, ensure_ascii=False)


def extract_courses(payloads):
    """Returnerar (lista med kurser, mängd med id:n för platser som matchar)."""
    pool = [(u, p) for u, p in payloads if "speedadmin" in u]
    course_pool = [(u, p) for u, p in pool if "cours" in u.lower()] or pool

    courses = []
    for _, p in course_pool:
        outermost_courses(p, courses)

    course_ids = {id(c) for c in courses}
    loc_ids = set()
    for _, p in pool:  # separata platslistor (id -> namn)
        for d in all_dicts(p):
            if id(d) in course_ids or not is_course(d):
                continue
            txt = dump(d).lower()
            if len(txt) < 1500 and any(k in txt for k in LOCATION_KEYWORDS):
                loc_ids.add(str(first_key(d, ID_KEYS)))
    return courses, loc_ids


def matches_location(course, loc_ids):
    txt = dump(course).lower()
    if any(k in txt for k in LOCATION_KEYWORDS):
        return True
    for k, v in course.items():
        if LOC_KEY_RE.search(k) and k.lower().endswith(("id", "ids")):
            vals = v if isinstance(v, list) else [v]
            if any(str(x) in loc_ids for x in vals):
                return True
    return False


def age_ranges(text):
    t = text.lower()
    ranges = []
    for a, b in re.findall(r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s*år", t):
        ranges.append((int(a), int(b)))
    for a in re.findall(r"(?:från|fr\.o\.m\.?)\s*(\d{1,2})\s*år", t):
        ranges.append((int(a), 99))
    for a in re.findall(r"(\d{1,2})\s*år\s*(?:eller|och)\s*(?:äldre|uppåt)", t):
        ranges.append((int(a), 99))
    lo = re.search(r"min_?age\W+(\d{1,2})", t)
    hi = re.search(r"max_?age\W+(\d{1,2})", t)
    if lo:
        ranges.append((int(lo.group(1)), int(hi.group(1)) if hi else 99))
    return ranges


def matches_age(course):
    rs = age_ranges(dump(course))
    if not rs:
        return True, False  # ålder okänd -> ta med, men markera
    return any(lo <= a <= hi for lo, hi in rs for a in AGES), True


def seats_available(course):
    """True/False om det går att utläsa, annars None."""
    for k, v in course.items():
        if FULL_KEY_RE.search(k) and isinstance(v, bool):
            return not v
    for k, v in course.items():
        if SEAT_KEY_RE.search(k) and isinstance(v, (int, float)) and not isinstance(v, bool):
            return v > 0
    return None


def course_label(course, age_known):
    name = first_key(course, NAME_KEYS)
    return name if age_known else f"{name} (ålder ej angiven)"


def select_matches(payloads):
    courses, loc_ids = extract_courses(payloads)
    result = {}
    for c in courses:
        if not matches_location(c, loc_ids):
            continue
        ok, known = matches_age(c)
        if ok:
            result[str(first_key(c, ID_KEYS))] = {
                "label": course_label(c, known),
                "available": seats_available(c),
            }
    return len(courses), result


# ------------------------------ tillstånd ----------------------------------
def load_state():
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state.setdefault("courses", None)   # None = ingen tidigare körning
    state.setdefault("failures", 0)
    state.setdefault("warned", False)
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def diff_and_notify(current, previous):
    """Jämför med förra kontrollen och notifierar. Returnerar nytt kursläge."""
    if previous is None:  # första kontrollen: skicka en sammanfattning
        notify(
            "Bevakning igång",
            f"Hittade {len(current)} matchande kurser just nu. Du får notis när nya kommer "
            f"eller när platser blir lediga.",
            priority=3,
            tags=["eyes"],
        )
        return current

    for key, c in current.items():
        old = previous.get(key)
        if old is None:
            avail = " - lediga platser!" if c["available"] else ""
            notify("Ny kurs på Kungsholmen", f"{c['label']}{avail}", priority=5, tags=["tada"])
        elif old.get("available") is False and c["available"] is True:
            notify("Plats ledig!", c["label"], priority=5, tags=["rotating_light"])
    return current


# ------------------------------ huvudlogik ---------------------------------
def shape(o, indent=0, depth=4):
    """Kort beskrivning av datans struktur (nycklar och typer)."""
    pad = "  " * indent
    if depth == 0:
        return pad + type(o).__name__
    if isinstance(o, dict):
        lines = []
        for k, v in list(o.items())[:30]:
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {type(v).__name__}({len(v)})")
                lines.append(shape(v, indent + 1, depth - 1))
            else:
                lines.append(f"{pad}{k}: {repr(v)[:70]}")
        return "\n".join(lines)
    if isinstance(o, list):
        return f"{pad}[första av {len(o)}]\n" + shape(o[0], indent + 1, depth) if o else pad + "[]"
    return pad + repr(o)[:70]


def discover():
    payloads = fetch_payloads()
    print(f"{len(payloads)} JSON-svar fångade:")
    for url, p in payloads:
        print(f"  {len(dump(p)):>8} tecken  {url}")
    for url, p in payloads:  # struktur för datakällor (inte sessions- eller språkfiler)
        if "session" in url or "locales" in url:
            continue
        print(f"\n=== STRUKTUR: {url}")
        print(shape(p)[:3500])
    courses, _ = extract_courses(payloads)
    if courses:
        print("\n=== EXEMPEL PÅ EN TOLKAD KURS (rå JSON):")
        print(dump(courses[0])[:1500])
    n, matches = select_matches(payloads)
    print(f"\nTolkade {n} kurser, {len(matches)} matchar Kungsholmen + ålder {sorted(AGES)}:")
    for m in matches.values():
        print(f"  - {m['label']}  (platser lediga: {m['available']})")
    Path("discover_dump.json").write_text(
        json.dumps([{"url": u, "data": p} for u, p in payloads], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("\nRådata sparad i discover_dump.json")


def check_once(force=False):
    ok, why = should_run(force)
    if not ok:
        print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M}] Hoppar över: {why}")
        return
    state = load_state()
    try:
        n, matches = select_matches(fetch_payloads())
        if n == 0:
            raise RuntimeError("Hittade inga kurser i katalogen - ändrad sidstruktur?")
        print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M}] {n} kurser tolkade, {len(matches)} matchar")
        state["courses"] = diff_and_notify(matches, state["courses"])
        state["failures"], state["warned"] = 0, False
    except Exception as e:  # nätfel, ändrad sida osv. Misslyckas tyst men varnar en gång.
        state["failures"] += 1
        print(f"Fel ({state['failures']} i rad): {e}", file=sys.stderr)
        if state["failures"] >= 3 and not state["warned"]:
            notify("Bevakningen har problem", f"{state['failures']} fel i rad: {e}", priority=4, tags=["warning"])
            state["warned"] = True
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="skicka testnotis")
    ap.add_argument("--discover", action="store_true", help="visa vad katalogen levererar")
    ap.add_argument("--loop", action="store_true", help="kör kontinuerligt")
    ap.add_argument("--force", action="store_true", help="ignorera tider och perioder")
    args = ap.parse_args()

    if args.test:
        notify("Test från Kulturskole-bevakaren", "Om du ser detta fungerar notiserna.", tags=["white_check_mark"])
        print("Testnotis skickad.")
    elif args.discover:
        discover()
    elif args.loop:
        while True:
            check_once(args.force)
            time.sleep(INTERVAL_SECONDS)
    else:
        check_once(args.force)


if __name__ == "__main__":
    main()
