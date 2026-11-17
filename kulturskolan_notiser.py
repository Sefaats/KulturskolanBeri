#!/usr/bin/env python3
"""
Bevakar Kulturskolan Stockholms kurskatalog och skickar notiser via ntfy.

Katalogens lista innehåller kurstyper (t.ex. "Piano - långa kurser") men inte
plats, ålder eller lediga platser, så bevakningen kan inte filtrera på
Kungsholmen eller födelseår. I stället får du notis när:
  1. bannertexten i katalogen ändras (t.ex. "vårens långa kurser är öppna"),
  2. nya kurstyper dyker upp i katalogen,
  3. en kurstyp blir direktbokningsbar.
Använd sedan filtren Födelseår 2020 och Stadsdel Kungsholmen i katalogen.

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
    python kulturskolan_notiser.py --discover    # utforska katalogens dataanrop
    python kulturskolan_notiser.py               # en kontroll (för schema/cron)
    python kulturskolan_notiser.py --loop        # kontrollerar var 10:e minut
    Lägg till --force för att köra utanför tider/perioder (t.ex. vid test).
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
STATE_VERSION = 3   # höj när tolkningen ändras, så nollställs gammalt tillstånd
# --------------------------------------------------------------------------

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
def dump(d):
    return json.dumps(d, ensure_ascii=False)


def find_payload(payloads, needle):
    for url, p in payloads:
        if needle in url.split("?")[0]:
            return p
    return None


def read_signals(payloads):
    """Plockar ut bannertext och kurstyper ur katalogens dataanrop."""
    cat = find_payload(payloads, "getcoursecatalogueofferingbasesbyfilter")
    if not isinstance(cat, dict) or not isinstance(cat.get("entities"), list):
        raise RuntimeError("Hittade inte kurslistan i katalogen - ändrad sidstruktur?")
    page = find_payload(payloads, "/getpage")
    data = page.get("data") if isinstance(page, dict) else None
    data = data if isinstance(data, dict) else {}

    banner = " ".join(str(data.get("spotsTitle") or "").split())
    spots = " | ".join(
        " ".join(str(sp.get(k) or "") for k in ("headline1", "description")).strip()
        for sp in (data.get("spots") or [])
        if isinstance(sp, dict)
    )
    offerings = {}
    for e in cat["entities"]:
        if isinstance(e, dict) and e.get("offeringBaseId") is not None:
            offerings[str(e["offeringBaseId"])] = {
                "name": str(e.get("publicName") or ""),
                "direct": bool(e.get("hasDirectBooking")),
            }
    return {"banner": banner, "spots": " ".join(spots.split()), "offerings": offerings}


# ------------------------------ tillstånd ----------------------------------
def load_state():
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if state.get("version") != STATE_VERSION:
            state = {}
    state["version"] = STATE_VERSION
    state.setdefault("signals", None)   # None = ingen tidigare körning
    state.setdefault("failures", 0)
    state.setdefault("warned", False)
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def diff_and_notify(cur, prev):
    """Jämför med förra kontrollen och notifierar. Returnerar nytt läge."""
    if prev is None:  # första kontrollen: skicka en sammanfattning
        notify(
            "Bevakning igång",
            f"{len(cur['offerings'])} kurstyper i katalogen just nu. "
            f"Katalogen säger: {cur['banner'] or '(ingen bannertext)'}",
            priority=3,
            tags=["eyes"],
        )
        return cur

    if (cur["banner"], cur["spots"]) != (prev.get("banner"), prev.get("spots")):
        notify("Kulturskolans katalog har ändrats", cur["banner"] or cur["spots"][:300],
               priority=5, tags=["rotating_light"])

    old = prev.get("offerings", {})
    new_names = [o["name"] for k, o in cur["offerings"].items() if k not in old]
    if new_names:
        more = f" (+{len(new_names) - 15} till)" if len(new_names) > 15 else ""
        notify(f"{len(new_names)} nya kurstyper i katalogen", ", ".join(new_names[:15]) + more,
               priority=5, tags=["tada"])

    opened = [o["name"] for k, o in cur["offerings"].items()
              if k in old and not old[k].get("direct") and o["direct"]]
    if opened:
        notify("Bokning öppen", ", ".join(opened[:15]), priority=5, tags=["white_check_mark"])
    return cur


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


def _noise(url):
    return "locales" in url or "session" in url


def discover():
    """Utforskar katalogen som en besökare och skriver ut vilka dataanrop som görs."""
    from playwright.sync_api import sync_playwright

    payloads, reqs = [], []
    stage = {"name": "1"}

    def on_request(req):
        u = req.url
        if "speedadmin.dk" in u and not _noise(u) and req.resource_type in ("xhr", "fetch"):
            reqs.append((stage["name"], req.method, u, (req.post_data or "")[:1500]))

    def on_response(resp):
        try:
            if "json" in (resp.headers.get("content-type") or ""):
                payloads.append((stage["name"], resp.url, resp.json()))
        except Exception:
            pass

    def show_new(r0, p0):
        for st, m, u, body in reqs[r0:]:
            print(f"  ANROP [{st}] {m} {u}")
            if body:
                print(f"      BODY: {body}")
        for st, u, pl in payloads[p0:]:
            if _noise(u):
                continue
            print(f"  SVAR {u}  ({len(dump(pl))} tecken)")
            print(shape(pl, indent=2, depth=4)[:2500])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(locale="sv-SE").new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        def step(label, fn, text=False):
            stage["name"] = label
            r0, p0 = len(reqs), len(payloads)
            print(f"\n##### STEG {label}")
            try:
                fn()
                page.wait_for_timeout(2500)
            except Exception as e:
                print(f"  (misslyckades: {str(e)[:200]})")
            show_new(r0, p0)
            if text:
                try:
                    print("  --- SYNLIG TEXT:")
                    print("  " + page.inner_text("body")[:3000].replace("\n", "\n  "))
                except Exception:
                    pass

        step("1 sidladdning", lambda: page.goto(CATALOGUE_URL, wait_until="networkidle", timeout=60000), text=False)

        for _, u, pl in payloads:  # bannertexten i sin helhet
            if u.endswith("/getpage") and isinstance(pl, dict):
                data = pl.get("data", {}) or {}
                print("\nBANNER:", data.get("spotsTitle"))
                for sp in data.get("spots", []) or []:
                    print("SPOT:", sp.get("headline1"), "|", str(sp.get("description"))[:1500])

        def describe_filters():
            labels = ["Ämne", "Födelseår", "Stadsdel", "Plats", "Veckodag", "Kursnivå", "Visa bokningsbara tider"]
            res = page.evaluate(
                """(labels) => {
                  const out = [];
                  for (const el of document.querySelectorAll('label,button,span,div,p,legend,h2,h3,h4')) {
                    const t = (el.textContent || '').trim();
                    if (labels.includes(t) && el.children.length < 3) {
                      const par = el.parentElement ? el.parentElement.outerHTML.slice(0, 450) : '';
                      out.push(el.tagName + '.' + (el.className || '') + ' :: ' + par);
                    }
                  }
                  return out.slice(0, 14);
                }""",
                labels,
            )
            for r in res:
                print("  FILTERELEMENT:", r)

        def list_options():
            seen, out = set(), []
            for t in page.locator("[role=option], [role=listbox] *, li, label").all_inner_texts():
                t = " ".join(t.split())
                if t and len(t) <= 40 and t not in seen:
                    seen.add(t)
                    out.append(t)
            print("  ALTERNATIV:", out[:80])

        def click_text(*patterns, then_list=False):
            def run():
                for pat in patterns:
                    try:
                        page.get_by_text(re.compile(pat, re.I)).first.click(timeout=3000, force=True)
                        if then_list:
                            page.wait_for_timeout(800)
                            list_options()
                        return
                    except Exception:
                        continue
                raise RuntimeError(f"hittade inget att klicka på: {patterns}")
            return run

        def open_course():
            page.locator(".offering-base-card-header").first.click(timeout=5000, force=True)

        print("\n##### FILTER-ELEMENT I SIDAN")
        try:
            describe_filters()
        except Exception as e:
            print(f"  (misslyckades: {str(e)[:200]})")

        step("2 oppna Stadsdel", click_text("^stadsdel$", "stadsdel", then_list=True))
        step("3 valj Kungsholmen", click_text("^kungsholmen$", "kungsholm"))
        step("4 oppna Fodelsear", click_text("^födelseår$", "födelseår", then_list=True))
        step("5 valj 2020", click_text("^2020$"))
        step("6 Visa bokningsbara tider", click_text("visa bokningsbara tider"), text=True)
        step("7 oppna forsta kursen", open_course, text=True)
        print("\nSIDANS ADRESS NU:", page.url)
        browser.close()

    Path("discover_dump.json").write_text(
        json.dumps([{"url": u, "data": d} for _, u, d in payloads if not _noise(u)], ensure_ascii=False, indent=1),
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
        cur = read_signals(fetch_payloads())
        print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M}] {len(cur['offerings'])} kurstyper. Banner: {cur['banner'][:120]}")
        state["signals"] = diff_and_notify(cur, state["signals"])
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
