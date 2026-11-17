#!/usr/bin/env python3
"""
Bevakar Kulturskolan Stockholms kurskatalog och skickar notiser via ntfy.

Filter (samma som i katalogens egna filter):
  - Stadsdel: Kungsholmens stadsdelsområde (id 8)
  - Födelseår: 2020
  - Bara bokningsbara kurstyper ("Visa bokningsbara tider")

Du får notis när en kurstyp som matchar blir bokningsbar (ny eller åter ledig).
Kurstyper utan lediga platser, på annan plats eller för annan ålder ger ingen notis.

TVÅ BEVAKNINGAR (--scope)
  long   Långa kurser (allt som inte är korta kurser). Kollas var 30:e minut,
         var 5:e minut den 18 december, bara under ACTIVE_PERIODS. Notis också
         om bannertexten i katalogen ändras.
  short  Korta kurser, lovkurser, dagkollo och prova på. Kollas en gång per dag,
         året runt.

TIDER
  Inga kontroller och inga notiser mellan 20:00 och 08:00 (svensk tid).

KÖRNING
  Avsett att köras av GitHub Actions (se kulturskolan.yml). På en vanlig dator:
    pip install requests playwright && playwright install chromium
    export NTFY_TOPIC="ditt-hemliga-amnesnamn"
    python kulturskolan_notiser.py --test            # testnotis
    python kulturskolan_notiser.py --discover        # utforska katalogens dataanrop
    python kulturskolan_notiser.py --scope all --force   # kontrollera nu, ignorera tider
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

DISTRICT_IDS = [8]     # Kungsholmens stadsdelsområde
BIRTH_YEARS = [2020]   # barnet är fött 2020

# Kurstyper vars namn matchar räknas som "korta" och bevakas en gång per dag.
SHORT_RE = re.compile(r"korta|lovkurs|dagkollo|prova på", re.I)

TZ = ZoneInfo("Europe/Stockholm")
AWAKE_FROM, AWAKE_TO = 8, 20        # kontroller/notiser bara 08:00-20:00

# Perioder för bevakningen av långa kurser, som (start, slut) i "MM-DD".
# Perioder får gå över årsskiftet. Tom lista = alltid på. De korta kurserna
# bevakas alltid, oavsett period.
ACTIVE_PERIODS = [
    ("11-15", "01-31"),   # inför vårterminen (långa kurser öppnar 18 december)
    ("04-15", "06-15"),   # inför höstterminen
    ("08-01", "09-15"),   # terminsstart och eftersläpp av platser
]

INTERVAL_SECONDS = 1800             # bara för --loop
STATE_FILE = Path(__file__).with_name("kulturskolan_state.json")
STATE_VERSION = 4   # höj när tolkningen ändras, så nollställs gammalt tillstånd
# --------------------------------------------------------------------------

LIST_URL_PART = "getcoursecatalogueofferingbasesbyfilter"
DROP_HEADERS = {"content-length", "host", "connection", "accept-encoding"}


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


def should_run(scope, force=False, now=None):
    now = now or datetime.now(TZ)
    if force:
        return True, ""
    if scope == "long" and not in_active_period(now):
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
def fetch_catalogue():
    """Laddar katalogen och frågar efter bokningsbara kurstyper med våra filter.

    Sidans eget anrop till kurslistan fångas (med sina huvuden), och samma anrop
    skickas sedan igen med filtren Stadsdel, Födelseår och onlyAvailable.
    Returnerar (bannerdata, svar med bokningsbara, svar med alla).
    """
    from playwright.sync_api import sync_playwright

    seen, pages = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="sv-SE")
        page = ctx.new_page()

        def on_request(req):
            if LIST_URL_PART in req.url and "req" not in seen:
                seen["req"] = req

        def on_response(resp):
            try:
                if "/getpage" in resp.url and "json" in (resp.headers.get("content-type") or ""):
                    pages.append(resp.json())
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(CATALOGUE_URL, wait_until="networkidle", timeout=60000)
        if "req" not in seen:
            raise RuntimeError("Såg inget anrop till kurslistan - ändrad sidstruktur?")

        url = seen["req"].url
        headers = {k: v for k, v in seen["req"].all_headers().items() if k.lower() not in DROP_HEADERS}
        headers["content-type"] = "application/json"

        def query(only_available):
            body = {
                "onlyAvailable": only_available,
                "filterByBundle": False,
                "filterByBookingV2Id": False,
                "districtIds": DISTRICT_IDS,
                "years": BIRTH_YEARS,
            }
            r = ctx.request.post(url, data=json.dumps(body), headers=headers, timeout=30000)
            if not r.ok:
                raise RuntimeError(f"Kurslistan svarade {r.status}")
            return r.json()

        avail, everything = query(True), query(False)
        browser.close()
    return (pages[0] if pages else {}), avail, everything


# ----------------------------- tolka data ----------------------------------
def dump(d):
    return json.dumps(d, ensure_ascii=False)


def read_offerings(resp):
    ents = resp.get("entities") if isinstance(resp, dict) else None
    if not isinstance(ents, list):
        raise RuntimeError("Oväntat svar från kurslistan - ändrad sidstruktur?")
    return {
        str(e["offeringBaseId"]): str(e.get("publicName") or "")
        for e in ents
        if isinstance(e, dict) and e.get("offeringBaseId") is not None
    }


def read_banner(page_payload):
    data = page_payload.get("data") if isinstance(page_payload, dict) else None
    data = data if isinstance(data, dict) else {}
    title = " ".join(str(data.get("spotsTitle") or "").split())
    spots = " | ".join(
        " ".join(str(sp.get(k) or "") for k in ("headline1", "description")).strip()
        for sp in (data.get("spots") or [])
        if isinstance(sp, dict)
    )
    return title, " ".join(spots.split())


def split_scope(offerings, scope):
    want_short = scope == "short"
    return {k: n for k, n in offerings.items() if bool(SHORT_RE.search(n)) == want_short}


# ------------------------------ tillstånd ----------------------------------
def load_state():
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if state.get("version") != STATE_VERSION:
            state = {}
    state["version"] = STATE_VERSION
    state.setdefault("scopes", {})
    state.setdefault("failures", 0)
    state.setdefault("warned", False)
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


LABEL = {"long": "långa kurser", "short": "korta kurser"}


def diff_and_notify(scope, cur, prev):
    """Jämför med förra kontrollen och notifierar. Returnerar nytt läge."""
    names = list(cur["avail"].values())
    if prev is None:  # första kontrollen: skicka en sammanfattning
        now_txt = ", ".join(names[:10]) if names else "inga just nu"
        notify(
            f"Bevakning igång ({LABEL[scope]})",
            f"Bokningsbart på Kungsholmen för födda 2020: {now_txt}.",
            priority=3,
            tags=["eyes"],
        )
        return cur

    old = prev.get("avail", {})
    new = [n for k, n in cur["avail"].items() if k not in old]
    if new:
        more = f" (+{len(new) - 10} till)" if len(new) > 10 else ""
        notify(
            "Ledig plats: Kungsholmen, född 2020",
            ", ".join(new[:10]) + more,
            priority=5,
            tags=["tada"],
        )

    if scope == "long" and (cur.get("banner"), cur.get("spots")) != (prev.get("banner"), prev.get("spots")):
        notify("Kulturskolans katalog har ändrats", cur.get("banner") or cur.get("spots", "")[:300],
               priority=5, tags=["rotating_light"])
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


def check_once(scope="long", force=False):
    scopes = ["long", "short"] if scope == "all" else [scope]
    now = datetime.now(TZ)
    todo = []
    for sc in scopes:
        ok, why = should_run(sc, force, now)
        if ok:
            todo.append(sc)
        else:
            print(f"[{now:%Y-%m-%d %H:%M}] {LABEL[sc]}: hoppar över ({why})")
    if not todo:
        return

    state = load_state()
    try:
        page_payload, avail_resp, all_resp = fetch_catalogue()
        avail, everything = read_offerings(avail_resp), read_offerings(all_resp)
        banner, spots = read_banner(page_payload)
        print(f"[{now:%Y-%m-%d %H:%M}] Kungsholmen/2020: {len(avail)} bokningsbara av {len(everything)} kurstyper")
        for sc in todo:
            cur = {"avail": split_scope(avail, sc)}
            if sc == "long":
                cur["banner"], cur["spots"] = banner, spots
            print(f"  {LABEL[sc]}: bokningsbara = {list(cur['avail'].values())}")
            state["scopes"][sc] = diff_and_notify(sc, cur, state["scopes"].get(sc))
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
    ap.add_argument("--discover", action="store_true", help="utforska katalogens dataanrop")
    ap.add_argument("--loop", action="store_true", help="kör kontinuerligt")
    ap.add_argument("--force", action="store_true", help="ignorera tider och perioder")
    ap.add_argument("--scope", choices=["long", "short", "all"], default="long")
    args = ap.parse_args()

    if args.test:
        notify("Test från Kulturskole-bevakaren", "Om du ser detta fungerar notiserna.", tags=["white_check_mark"])
        print("Testnotis skickad.")
    elif args.discover:
        discover()
    elif args.loop:
        while True:
            check_once(args.scope, args.force)
            time.sleep(INTERVAL_SECONDS)
    else:
        check_once(args.scope, args.force)


if __name__ == "__main__":
    main()
