#!/usr/bin/env python3
"""LA City Pay Tennis availability monitor (alert only; never books).

  python monitor.py --login      # log in once by hand; saves session.json
  python monitor.py --discover   # save the current page + all frames to ./debug
  python monitor.py              # one check; notifies on NEW matching slots
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import yaml
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
CFG = yaml.safe_load((HERE / "config.yaml").read_text())
SEEN = HERE / "seen.json"
STATE = HERE / "session.json"  # saved login cookies; keep private, do not share
DEBUG = HERE / "debug"


def ctx_args():
    return {"storage_state": str(STATE)} if STATE.exists() else {}


def login():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        ctx = b.new_context()
        page = ctx.new_page()
        page.goto("https://recreation.parks.lacity.gov/myprofile")
        input("Log in in the browser window, then press Enter here... ")
        ctx.storage_state(path=str(STATE))
        STATE.chmod(0o600)
        print(f"Session saved to {STATE}")
        b.close()


def discover():
    DEBUG.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        page = b.new_context(**ctx_args()).new_page()
        page.goto(search_url(date.today() + timedelta(days=1)))
        input("Inspect the page, then press Enter to snapshot (do NOT click a time slot)... ")
        (DEBUG / "page.html").write_text(page.content())
        page.screenshot(path=str(DEBUG / "page.png"), full_page=True)
        b.close()


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
BASE_PAGE = CFG["search_url"] + "?module=FR&InterfaceParameter=Iframe_Live_WebTrac"


def search_url(d, token=""):
    """Same fields the site's own Search button submits."""
    c = CFG["criteria"]
    params = [("Action", "Start"), ("SubAction", ""), ("_csrf_token", token),
              ("date", d.strftime("%m/%d/%Y")), ("begintime", " " + c["earliest"].lstrip("0").lower())]
    params += [("location", loc) for loc in CFG["locations"]]
    params += [("keyword", ""), ("keywordoption", "Match One"), ("blockstodisplay", "10"),
               ("frheadcount", "0"), ("primarycode", ""), ("display", "Detail"), ("module", "FR"),
               ("multiselectlist_value", ""), ("frwebsearch_buttonsearch", "yes")]
    return CFG["search_url"] + "?" + urlencode(params)


def to_minutes(text):
    m = re.search(r"(\d{1,2}):?(\d{2})?\s*(AM|PM)", text, re.I)
    h, mi, ap = int(m[1]), int(m[2] or 0), m[3].upper()
    return (h % 12 + (12 if ap == "PM" else 0)) * 60 + mi


# Runs in the page: one record per OPEN ("Book Now") time block.
EXTRACT_JS = """
() => {
  const out = [];
  document.querySelectorAll('.result-content').forEach(rc => {
    const name = (rc.querySelector('h2') || {}).innerText || '';
    const loc = (rc.querySelector('td[data-title="Location Description"]') || {}).innerText || '';
    const day = rc.querySelector('.dateblock');
    const dstr = day ? day.getAttribute('data-tooltip') : '';
    rc.querySelectorAll('a.cart-button--state-block.success').forEach(a => {
      out.push({court: name.trim(), location: loc.trim(), date: dstr,
                label: a.innerText.replace(/\\s+/g, ' ').trim(), href: a.href});
    });
  });
  return out;
}
"""


def add_times(slot):
    q = parse_qs(urlparse(slot["href"]).query)
    b = int(q["GlobalSalesArea_FRItemBeginTime"][0]) // 60
    e = int(q["GlobalSalesArea_FRItemEndTime"][0]) // 60
    slot["start"], slot["minutes"] = b, e - b
    del slot["href"]
    return slot


def matches(slot):
    c = CFG["criteria"]
    d = datetime.strptime(slot["date"], "%m/%d/%Y")
    if d.strftime("%a") not in c["days"]:
        return False
    if not (to_minutes(c["earliest"]) <= slot["start"] <= to_minutes(c["latest"])):
        return False
    if slot["minutes"] < c["min_minutes"]:
        return False
    fc = c.get("facility_contains") or []
    return not fc or any(f.lower() in slot["court"].lower() for f in fc)


SIG_JS = """() => [...document.querySelectorAll('.result-content')].map(rc =>
  ((rc.querySelector('h2')||{}).innerText||'') + '@' +
  ((rc.querySelector('td[data-title="Location Description"]')||{}).innerText||'')).join('|')"""


def ev(page, js, arg=None):
    """page.evaluate that survives a mid-flight redirect/navigation."""
    for i in range(5):
        try:
            return page.evaluate(js, arg) if arg is not None else page.evaluate(js)
        except Exception as e:
            if "context was destroyed" not in str(e) and "navigation" not in str(e).lower():
                raise
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(300 * (i + 1))
    return page.evaluate(js)


def collect(page, d):
    """Read every page of results for one date (site shows 20 courts/page, numbered buttons).
    Returns (courts, open_blocks, pages)."""
    courts, found, pages, seen_sigs = 0, [], 0, set()
    for _ in range(30):
        sig = ev(page, SIG_JS)
        if sig in seen_sigs:  # page didn't change; stop
            break
        seen_sigs.add(sig)
        pages += 1
        courts += page.locator(".result-content").count()
        found += ev(page, EXTRACT_JS)
        cur = page.locator("button.paging__button.primary").first
        if cur.count() == 0:  # single page of results
            break
        nxt_no = str(int(cur.inner_text().strip()) + 1)
        nxt = page.locator(f"button.paging__button:text-is('{nxt_no}')").first
        if nxt.count() == 0:
            break
        nxt.click()
        # The site may reload the whole page on click, so poll (tolerating a destroyed
        # context) until a non-empty, different court list appears.
        changed, deadline = False, time.time() + 20
        while time.time() < deadline:
            page.wait_for_timeout(300)
            try:
                new = page.evaluate(SIG_JS)
            except Exception:
                continue
            if new and new != sig:
                changed = True
                break
        if not changed:
            print(f"  {d:%a %m/%d}: page {nxt_no} did not load - results INCOMPLETE")
            FAILED.append(d)
            break
        page.wait_for_load_state("domcontentloaded")
    # de-dupe in case a block shows on more than one page
    uniq = {(s["location"], s["court"], s["date"], s["label"]): s for s in found}
    return courts, list(uniq.values()), pages


WORKERS = 3          # parallel browsers; each handles a share of the dates
ATTEMPTS = 3         # tries per date before giving up on it
NAV_TIMEOUT = 45000  # ms to wait for a page before retrying
FAILED = []          # dates that could not be loaded this run (shown in the report)


def fetch_date(ctx, d, token):
    """One date, fresh tab, with retries. Returns (status, courts, found, pages, expired) or None."""
    for attempt in range(1, ATTEMPTS + 1):
        page = ctx.new_page()
        try:
            r = page.goto(search_url(d, token), wait_until="domcontentloaded",
                          referer=BASE_PAGE, timeout=NAV_TIMEOUT)
            if "login" in page.url.lower():
                return None, 0, [], 0, True
            try:  # wait for results (or move on quickly if there are none)
                page.wait_for_selector(".result-content", timeout=8000)
            except Exception:
                pass
            courts, found, pages = collect(page, d)
            if courts == 0:
                DEBUG.mkdir(exist_ok=True)
                (DEBUG / f"empty_{d:%m-%d}.html").write_text(page.content())
            return (r.status if r else "?"), courts, found, pages, False
        except Exception as e:
            print(f"  {d:%a %m/%d}: attempt {attempt}/{ATTEMPTS} failed ({type(e).__name__})")
            time.sleep(2 * attempt)
        finally:
            page.close()
    return None


def scrape_dates(days):
    """One browser: fetch the given dates. Returns (slots, log_lines, session_expired)."""
    slots, log = [], []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=CFG.get("headless", True),
                              args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=UA, **ctx_args())
        # Skip images/fonts/media: not needed to read availability.
        ctx.route("**/*", lambda r: r.abort()
                  if r.request.resource_type in ("image", "media", "font") else r.continue_())
        token = ""
        for attempt in range(ATTEMPTS):
            page = ctx.new_page()
            try:
                page.goto(BASE_PAGE, wait_until="networkidle", timeout=NAV_TIMEOUT)
                token = ev(page, "() => (document.querySelector('#_csrf_token') || {}).value || ''")
                page.close()
                break
            except Exception:
                page.close()
                time.sleep(2 * (attempt + 1))
        for d in days:
            res = fetch_date(ctx, d, token)
            if res is None:
                FAILED.append(d)
                log.append((d, f"  {d:%a %m/%d}: FAILED after {ATTEMPTS} attempts"))
                continue
            status, courts, found, pages, expired = res
            if expired:
                b.close()
                return slots, log, True
            log.append((d, f"  {d:%a %m/%d}: HTTP {status}, {courts} courts on {pages} page(s), "
                           f"{len(found)} open blocks" + ("  [WARNING: no courts]" if courts == 0 else "")))
            for s in found:
                s["search"] = search_url(d)
                slots.append(add_times(s))
        b.close()
    return slots, log, False


def scrape():
    from concurrent.futures import ThreadPoolExecutor
    days = [date.today() + timedelta(days=i) for i in range(CFG["criteria"]["days_ahead"] + 1)]
    days = [d for d in days if d.strftime("%a") in CFG["criteria"]["days"]]
    chunks = [days[i::WORKERS] for i in range(WORKERS) if days[i::WORKERS]]
    with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
        results = list(ex.map(scrape_dates, chunks))
    if any(expired for _, _, expired in results):
        notify("Tennis monitor", "Session expired. Run: python monitor.py --login")
        sys.exit(1)
    for _, line in sorted((x for _, log, _ in results for x in log), key=lambda x: x[0]):
        print(line)
    return [s for slots, _, _ in results for s in slots]


def fmt(s):
    t = datetime(2000, 1, 1) + timedelta(minutes=s["start"])
    return f"{s['location']} / {s['court']} - {s['date']} {t:%-I:%M %p} ({s['minutes']} min)"


DISPLAY = {"BalboaPayTennis": "Balboa Pay Tennis", "VNSO Pay Tennis": "Van Nuys/Sherman Oaks Pay Tennis"}
REPORT = HERE / "report.html"
CSS = """
body{font:15px -apple-system,Helvetica,sans-serif;background:#f6f1e8;color:#2b2b2b;margin:0;padding:24px}
h1{margin:0 0 4px;font-size:22px;color:#c8472a}.sub{color:#777;margin-bottom:10px;font-size:13px}
.legend{margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;font-size:13px;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}
.card{background:#fff;border-radius:10px;box-shadow:0 1px 3px #0002;overflow:hidden}
.card h2{margin:0;padding:12px 16px;font-size:16px;display:flex;justify-content:space-between;align-items:center;background:#c8472a;color:#fff}
.card.empty h2{background:#b9b3a8}.badge{background:#fff3;border-radius:12px;padding:2px 10px;font-size:12px;margin-left:4px}
.row{display:flex;gap:12px;padding:10px 16px;border-top:1px solid #eee}.d{width:74px;font-weight:600;flex:none}
.d small{display:block;font-weight:400;color:#888}.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:#eef0f1;border:1px solid #cfd4d7;border-radius:8px;padding:4px 8px;font-size:13px;text-decoration:none;color:#555}
.chip small{color:#8a9296}.chip.new{box-shadow:0 0 0 3px #f5c542}
.chip.pri0{background:#1e7d3a;border-color:#14612b;color:#fff;font-weight:600}.chip.pri0 small{color:#d4f1dc;font-weight:400}
.chip.pri1{background:#2b63c4;border-color:#1f4d9e;color:#fff;font-weight:600}.chip.pri1 small{color:#d6e4fb;font-weight:400}
.row.pri0{background:#f0faf2}.row.pri1{background:#eef4fd}
.card.hasp0{border-left:6px solid #1e7d3a}.card.hasp1{border-left:6px solid #2b63c4}
.badge.pri0{background:#1e7d3a;color:#fff}.badge.pri1{background:#2b63c4;color:#fff}
.none{padding:14px 16px;color:#999}
.dates{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 14px}
.dbtn{font:inherit;font-size:13px;background:#fff;border:2px solid #cfc8b8;border-radius:20px;padding:5px 12px;cursor:pointer;color:#444;display:flex;align-items:center;gap:6px}
.dbtn small{background:#0001;border-radius:10px;padding:0 7px;font-size:11px}
.dbtn.wkday{border-color:#9bd0a7}.dbtn.wkend{border-color:#9db9ea}
.dbtn.zero{opacity:.55}.dbtn:hover{background:#faf6ee}
.dbtn.on{background:#c8472a;border-color:#c8472a;color:#fff}.dbtn.on small{background:#fff3}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}.dot.pri0{background:#1e7d3a}.dot.pri1{background:#2b63c4}
.dbtn.on .dot{box-shadow:0 0 0 2px #fff}
"""
ICONS = ["&#9790;", "&#9728;"]  # moon (evening), sun (morning); extra rules reuse the last icon
DEFAULT_PRIORITY = [
    {"label": "Mon-Thu evenings", "days": ["Mon", "Tue", "Wed", "Thu"], "start": "5:00 PM", "end": "8:00 PM"},
    {"label": "Weekend mornings", "days": ["Sat", "Sun"], "start": "8:00 AM", "end": "11:00 AM"},
]


FILTER_JS = """
(function(){
var ICONS=__ICONS__, KEY='tennis_sel';
var cards=[].slice.call(document.querySelectorAll('.card'));
var btns=[].slice.call(document.querySelectorAll('.dbtn'));
function apply(sel, label){
  [].slice.call(document.querySelectorAll('.row')).forEach(function(r){
    r.style.display=(sel==='all'||r.getAttribute('data-date')===sel)?'':'none';});
  cards.forEach(function(c){
    var n=0, pc={}, minp=null;
    [].slice.call(c.querySelectorAll('.row')).forEach(function(r){
      if(r.style.display==='none') return;
      [].slice.call(r.querySelectorAll('.chip')).forEach(function(ch){
        n+=parseInt(ch.getAttribute('data-n')||'0',10);
        var p=ch.getAttribute('data-p');
        if(p!==null&&p!==''){pc[p]=(pc[p]||0)+1; if(minp===null||+p<minp) minp=+p;}
      });
    });
    var h=n?(n+' open'):'none';
    Object.keys(pc).sort().forEach(function(p){
      h+='<span class="badge pri'+p+'">'+ICONS[Math.min(+p,ICONS.length-1)]+' '+pc[p]+'</span>';});
    c.querySelector('.bdg').innerHTML=h;
    c.className=c.className.replace(/\\s*(empty|hasp\\d)/g,'');
    if(!n) c.className+=' empty';
    if(minp!==null) c.className+=' hasp'+minp;
    var m=c.querySelector('.msg');
    m.style.display=n?'none':'';
    m.textContent=(sel==='all')?'No matching slots':('No slots on '+label);
  });
  btns.forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-date')===sel); });
  try{localStorage.setItem(KEY, sel);}catch(e){}
}
btns.forEach(function(b){ b.addEventListener('click', function(){
  apply(b.getAttribute('data-date'), b.getAttribute('data-label')); }); });
var start='all', lab='';
try{ var s=localStorage.getItem(KEY); }catch(e){ var s=null; }
btns.forEach(function(b){ if(s && b.getAttribute('data-date')===s){ start=s; lab=b.getAttribute('data-label'); } });
apply(start, lab);
})();
"""


def build_report(hits, new_keys, key):
    from html import escape as e
    c = CFG["criteria"]
    rules = c.get("priority") or DEFAULT_PRIORITY
    icon = lambda i: ICONS[min(i, len(ICONS) - 1)]

    def pri_rule(s):
        """Index of the priority rule this block falls under (block STARTS in [start, end)), else None."""
        wd = datetime.strptime(s["date"], "%m/%d/%Y").strftime("%a")
        for i, r in enumerate(rules):
            if wd in r["days"] and to_minutes(r["start"]) <= s["start"] < to_minutes(r["end"]):
                return i
        return None

    by_loc = {}
    for s in hits:
        by_loc.setdefault(s["location"], []).append(s)
    cards = []
    for name in [DISPLAY.get(l, l) for l in CFG["locations"]]:
        slots = by_loc.get(name, [])
        body = ""
        for d in sorted({s["date"] for s in slots}, key=lambda x: datetime.strptime(x, "%m/%d/%Y")):
            dt = datetime.strptime(d, "%m/%d/%Y")
            chips, row_rule = "", None
            for st in sorted({s["start"] for s in slots if s["date"] == d}):
                grp = [s for s in slots if s["date"] == d and s["start"] == st]
                t = datetime(2000, 1, 1) + timedelta(minutes=st)
                courts = ", ".join(e(s["court"].split(" - ")[0].replace("Court ", "Ct ")) for s in grp)
                p = pri_rule(grp[0])
                if p is not None and row_rule is None:
                    row_rule = p
                cls = "chip" + (f" pri{p}" if p is not None else "") + \
                      (" new" if any(key(s) in new_keys for s in grp) else "")
                mark = f"{icon(p)} " if p is not None else ""
                chips += (f'<a class="{cls}" data-n="{len(grp)}" data-p="{"" if p is None else p}" '
                          f'href="{e(grp[0]["search"])}" target="_blank">'
                          f'{mark}{t:%-I:%M %p} <small>{courts}</small></a>')
            rcls = "row" + (f" pri{row_rule}" if row_rule is not None else "")
            body += (f'<div class="{rcls}" data-date="{d}"><div class="d">{dt:%a}<small>{dt:%b %-d}</small>'
                     f'</div><div class="chips">{chips}</div></div>')
        counts = {}
        for k in {(s["date"], s["start"]) for s in slots}:
            p = pri_rule({"date": k[0], "start": k[1]})
            if p is not None:
                counts[p] = counts.get(p, 0) + 1
        cls = ("card" if slots else "card empty") + (f" hasp{min(counts)}" if counts else "")
        badge = f"{len(slots)} open" if slots else "none"
        for p in sorted(counts):
            badge += f'<span class="badge pri{p}">{icon(p)} {counts[p]}</span>'
        msg = ('<div class="none msg">No matching slots</div>' if not slots
               else '<div class="none msg" style="display:none"></div>')
        cards.append(f'<div class="{cls}"><h2>{e(name)}<span class="bdg">{badge}</span></h2>{body}{msg}</div>')

    # Date buttons: every scanned day in the window (so "nothing open" days are selectable too)
    failed = {d for d in FAILED}
    btns = '<button class="dbtn on" data-date="all" data-label="all dates">All dates</button>'
    for i in range(c["days_ahead"] + 1):
        d = date.today() + timedelta(days=i)
        if d.strftime("%a") not in c["days"] or d in failed:
            continue
        ds = d.strftime("%m/%d/%Y")
        day_hits = [s for s in hits if s["date"] == ds]
        n = len(day_hits)
        ps = [pri_rule(s) for s in day_hits if pri_rule(s) is not None]
        dot = f'<i class="dot pri{min(ps)}"></i>' if ps else ""
        wk = "wkend" if d.weekday() >= 5 else "wkday"
        btns += (f'<button class="dbtn {wk}{" zero" if not n else ""}" data-date="{ds}" '
                 f'data-label="{d:%a %b %-d}">{d:%a %-m/%-d}{dot}<small>{n}</small></button>')

    warn = ""
    if FAILED:
        ds_ = ", ".join(f"{d:%a %b %-d}" for d in sorted(set(FAILED)))
        warn = (f'<div style="background:#fde8e4;border:1px solid #e0a79c;color:#8a2b17;border-radius:8px;'
                f'padding:8px 12px;margin-bottom:12px;font-size:13px">&#9888; Could not fully load: {ds_}. '
                f'Results for those dates may be incomplete; re-run to retry.</div>')
    legend = "".join(
        f'<span class="chip pri{i}">{icon(i)} {e(r["label"])} &middot; {e(r["start"])}&ndash;{e(r["end"])}</span>'
        for i, r in enumerate(rules))
    sub = (f"Updated {datetime.now():%a %b %-d, %-I:%M %p} &middot; {', '.join(c['days'])} "
           f"&middot; starts {e(c['earliest'])}&ndash;{e(c['latest'])} &middot; "
           f"{len(hits)} open block(s), {len(new_keys)} new &middot; gold outline = new since last run")
    js = FILTER_JS.replace("__ICONS__", json.dumps(ICONS))
    return (f"<!doctype html><meta charset=utf-8><title>Tennis availability</title><style>{CSS}</style>"
            f"<h1>Pay Tennis availability</h1><div class=sub>{sub}</div>{warn}"
            f"<div class=dates>{btns}</div>"
            f"<div class=legend>Priority: {legend}</div><div class=grid>{''.join(cards)}</div>"
            f"<script>{js}</script>")


def notify(title, body):
    n = CFG["notify"]
    if n.get("macos", True):
        subprocess.run(["osascript", "-e",
                        f"display notification {json.dumps(body)} with title {json.dumps(title)}"])
    if n.get("ntfy_topic"):
        subprocess.run(["curl", "-s", "-d", body, f"https://ntfy.sh/{n['ntfy_topic']}"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--login", action="store_true")
    args = ap.parse_args()
    if args.login:
        return login()
    if args.discover:
        return discover()

    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    hits = [s for s in scrape() if matches(s)]
    key = lambda s: json.dumps({k: v for k, v in s.items() if k != "search"}, sort_keys=True)
    new = [s for s in hits if key(s) not in seen]
    REPORT.write_text(build_report(hits, {key(s) for s in new}, key))
    print(f"{datetime.now():%F %T}: {len(hits)} matching, {len(new)} new -> {REPORT}")
    if new and CFG["notify"].get("open_report"):
        subprocess.run(["open", str(REPORT)])
    for s in new:
        print("  NEW", fmt(s))
    if new:
        body = "\n".join(fmt(s) for s in new[:8])
        if len(new) > 8:
            body += f"\n+{len(new) - 8} more"
        notify("Tennis court available", body)
        SEEN.write_text(json.dumps(sorted(seen | {key(s) for s in new})))


if __name__ == "__main__":
    main()
