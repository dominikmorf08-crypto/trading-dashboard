#!/usr/bin/env python3
"""Opening-Range-Breakout-Dashboard.

Prueft fuer Krypto, Indizes, Metalle, Energie und Devisen, ob der Kurs aus der
Eroeffnungsspanne der London-Session (09:00 Schweizer Zeit) und der
New-York-Session (15:30 Schweizer Zeit) ausbricht, setzt Stop und Ziel, misst
dieselbe Regel an den letzten rund 60 Tagen und schreibt eine Webseite
(index.html) plus data.json.

Nur Python-Standardbibliothek. Daten: Yahoo Finance (kostenlos, inoffiziell,
verzoegert). Keine Anlageberatung. Es wird nichts gekauft oder verkauft.

    python orb.py                     # live, schreibt nach ./site
    python orb.py --out site --now "2026-10-22 16:10"   # Uhrzeit (Schweizer Zeit) festlegen
    python orb.py --offline testdata  # gespeicherte Yahoo-JSONs statt Internet
"""

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc

# --------------------------------------------------------------------------
# Einstellungen
# --------------------------------------------------------------------------

TARGET_R = 2.0          # Ziel = Einstieg + 2 x Risiko
MIN_RISK_PCT = 0.0005   # Risiko unter 0,05 % des Kurses: Spanne zu eng
MIN_HISTORY = 8         # so viele vergangene Signale braucht eine Rangierung
CHASE_LIMIT_R = 0.75    # weiter als 0,75 R gelaufen: nicht mehr "frisch"

# (Yahoo-Symbol, Anzeigename, Gruppe, Nachkommastellen, Hinweis)
MARKETS = [
    ("BTC-USD", "Bitcoin", "Krypto", 0, ""),
    ("ETH-USD", "Ethereum", "Krypto", 1, ""),
    ("SOL-USD", "Solana", "Krypto", 2, ""),
    ("XRP-USD", "XRP", "Krypto", 4, ""),
    ("NQ=F", "Nasdaq 100", "Indizes", 2, "Future, CFD NAS100 weicht leicht ab"),
    ("ES=F", "S&P 500", "Indizes", 2, "Future, CFD US500 weicht leicht ab"),
    ("YM=F", "Dow Jones", "Indizes", 0, "Future, CFD US30 weicht leicht ab"),
    ("RTY=F", "Russell 2000", "Indizes", 2, "Future"),
    ("^GDAXI", "DAX", "Indizes", 2, "Kassa, handelt 09:00-17:30"),
    ("^SSMI", "SMI", "Indizes", 2, "Kassa, handelt 09:00-17:30"),
    ("GC=F", "Gold (XAU/USD)", "Rohstoffe", 2, "Future, liegt einige $ über Spot"),
    ("SI=F", "Silber (XAG/USD)", "Rohstoffe", 3, "Future"),
    ("CL=F", "Öl WTI", "Rohstoffe", 2, "Future"),
    ("BZ=F", "Öl Brent", "Rohstoffe", 2, "Future"),
    ("NG=F", "Erdgas", "Rohstoffe", 3, "Future"),
    ("HG=F", "Kupfer", "Rohstoffe", 4, "Future"),
    ("EURUSD=X", "EUR/USD", "Devisen", 5, ""),
    ("GBPUSD=X", "GBP/USD", "Devisen", 5, ""),
    ("USDJPY=X", "USD/JPY", "Devisen", 3, ""),
    ("USDCHF=X", "USD/CHF", "Devisen", 5, ""),
    ("AUDUSD=X", "AUD/USD", "Devisen", 5, ""),
    ("USDCAD=X", "USD/CAD", "Devisen", 5, ""),
    ("NZDUSD=X", "NZD/USD", "Devisen", 5, ""),
    ("EURCHF=X", "EUR/CHF", "Devisen", 5, ""),
    ("EURGBP=X", "EUR/GBP", "Devisen", 5, ""),
    ("EURJPY=X", "EUR/JPY", "Devisen", 3, ""),
    ("GBPJPY=X", "GBP/JPY", "Devisen", 3, ""),
]

SESSIONS = [
    # key, Name, Zeitzone, Start, Spanne (Min.), letzter Einstieg, Ende
    dict(key="london", name="London", tz="uk", start=(8, 0), or_min=15,
         last_entry=(11, 30), end=(16, 30)),
    dict(key="ny", name="New York", tz="us", start=(9, 30), or_min=15,
         last_entry=(15, 30), end=(16, 0)),
]

BAR = 300  # 5-Minuten-Kerzen

# Kassa-Indizes schliessen um 17:30: nur London-Session
LONDON_ONLY = {"^GDAXI", "^SSMI"}

# --------------------------------------------------------------------------
# Zeitzonen ohne tz-Datenbank (laeuft auch unter Windows ohne Zusatzpaket)
# --------------------------------------------------------------------------


def _nth_sunday(y, m, n):
    d = date(y, m, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_sunday(y, m):
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _us_offset(t):
    a = _nth_sunday(t.year, 3, 2)
    b = _nth_sunday(t.year, 11, 1)
    start = datetime(a.year, a.month, a.day, 7, tzinfo=UTC)
    end = datetime(b.year, b.month, b.day, 6, tzinfo=UTC)
    return -4 if start <= t < end else -5


def _eu_dst(t):
    a = _last_sunday(t.year, 3)
    b = _last_sunday(t.year, 10)
    start = datetime(a.year, a.month, a.day, 1, tzinfo=UTC)
    end = datetime(b.year, b.month, b.day, 1, tzinfo=UTC)
    return start <= t < end


OFFSETS = {
    "us": _us_offset,
    "uk": lambda t: 1 if _eu_dst(t) else 0,
    "ch": lambda t: 2 if _eu_dst(t) else 1,
}


def local(ts, tz):
    t = datetime.fromtimestamp(ts, UTC)
    return (t + timedelta(hours=OFFSETS[tz](t))).replace(tzinfo=None)


def local_to_ts(d, hh, mm, tz):
    guess = datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC)
    off = OFFSETS[tz](guess)
    t = guess - timedelta(hours=off)
    off2 = OFFSETS[tz](t)
    if off2 != off:
        t = guess - timedelta(hours=off2)
    return int(t.timestamp())


def ch_time(ts):
    return local(ts, "ch").strftime("%H:%M")


# --------------------------------------------------------------------------
# Daten
# --------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(symbol, offline=None):
    if offline:
        path = os.path.join(offline, symbol.replace("^", "_").replace("=", "_") + ".json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        q = urllib.parse.quote(symbol)
        data, last_err = None, None
        for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
            for attempt in range(2):
                try:
                    data = _get(f"https://{host}/v8/finance/chart/{q}"
                                "?range=60d&interval=5m&includePrePost=false")
                    break
                except urllib.error.HTTPError as e:
                    last_err = e
                    if e.code == 429:
                        time.sleep(5)
                        continue
                    break
                except Exception as e:  # Netzwerk
                    last_err = e
                    break
            if data:
                break
        if not data:
            raise RuntimeError(str(last_err))
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
        if None in (o, h, l, c) or t % BAR:
            continue
        bars.append((t, o, h, l, c))
    bars.sort()
    meta = res.get("meta", {})
    live = None
    if meta.get("regularMarketPrice") and meta.get("regularMarketTime"):
        live = (meta["regularMarketTime"], meta["regularMarketPrice"])
    return bars, live


# --------------------------------------------------------------------------
# Die Regel
# --------------------------------------------------------------------------


def run_session(bars, d, s, now=None, live=None):
    """Eroeffnungsspanne der Session s am Tag d (Ortszeit der Session)."""
    tz = s["tz"]
    t0 = local_to_ts(d, *s["start"], tz)
    t_or = t0 + s["or_min"] * 60
    t_last = local_to_ts(d, *s["last_entry"], tz)
    t_end = local_to_ts(d, *s["end"], tz)
    cut = now if now is not None else float("inf")
    r = dict(date=d.isoformat(), t0=t0, t_or=t_or, t_last=t_last, t_end=t_end)

    if cut < t0:
        r["status"] = "pending"
        return r
    closed = [b for b in bars if t0 <= b[0] < t_end and b[0] + BAR <= cut]
    or_bars = [b for b in closed if b[0] < t_or]
    if len(or_bars) < s["or_min"] // 5:
        r["status"] = "forming" if cut < t_or + BAR * 2 else "nodata"
        return r
    hi = max(b[2] for b in or_bars)
    lo = min(b[3] for b in or_bars)
    mid = (hi + lo) / 2
    r.update(hi=hi, lo=lo, mid=mid)
    last_close = closed[-1][4]
    r["last"] = last_close

    trig = None
    for b in closed:
        if b[0] < t_or:
            continue
        if b[0] + BAR > t_last:
            break
        if b[4] > hi:
            trig = (b, 1)
            break
        if b[4] < lo:
            trig = (b, -1)
            break
    if not trig:
        r["status"] = "expired" if cut >= t_last else "waiting"
        return r

    b, side = trig
    entry = b[4]
    stop = mid
    risk = abs(entry - stop)
    r.update(side=side, entry=entry, stop=stop, risk=risk, trig_ts=b[0] + BAR)
    if risk < entry * MIN_RISK_PCT:
        r["status"] = "too_tight"
        return r
    target = entry + side * TARGET_R * risk
    r["target"] = target

    for x in closed:
        if x[0] <= b[0]:
            continue
        hit_stop = x[3] <= stop if side > 0 else x[2] >= stop
        hit_tgt = x[2] >= target if side > 0 else x[3] <= target
        if hit_stop:      # beide in einer Kerze: zaehlt als Stop
            r.update(status="stop", R=-1.0, exit_ts=x[0] + BAR)
            return r
        if hit_tgt:
            r.update(status="target", R=TARGET_R, exit_ts=x[0] + BAR)
            return r

    price = last_close
    if (live and now is not None and cut < t_end
            and closed[-1][0] + BAR <= live[0] <= cut):
        price = live[1]
    r["price"] = price
    r["R"] = side * (price - entry) / risk
    r["status"] = "open" if cut < t_end else "closed"
    return r


def history(bars, s, today):
    days = sorted({local(b[0], s["tz"]).date() for b in bars})
    out = {1: [], -1: []}
    for d in days:
        if d >= today:
            continue
        r = run_session(bars, d, s)
        if r.get("status") in ("stop", "target", "closed", "open"):
            out[r["side"]].append(r["R"])
    stats = {}
    for side, rs in out.items():
        n = len(rs)
        stats[side] = dict(
            n=n,
            target=sum(1 for x in rs if x >= TARGET_R),
            stop=sum(1 for x in rs if x <= -1),
            avgR=round(sum(rs) / n, 3) if n else None,
        )
        stats[side]["neither"] = n - stats[side]["target"] - stats[side]["stop"]
    return stats


# --------------------------------------------------------------------------
# Auswertung
# --------------------------------------------------------------------------


def evaluate(now, offline=None, pause=0.3):
    rows, errors = [], []
    for sym, name, group, dec, note in MARKETS:
        try:
            bars, live = fetch(sym, offline)
        except Exception as e:
            errors.append(f"{sym}: {e}")
            for s in SESSIONS:
                rows.append(dict(symbol=sym, name=name, group=group, dec=dec,
                                 note=note, session=s["key"], status="error"))
            continue
        for s in SESSIONS:
            if sym in LONDON_ONLY and s["key"] != "london":
                continue
            today = local(now, s["tz"]).date()
            r = run_session(bars, today, s, now=now, live=live)
            r.update(symbol=sym, name=name, group=group, dec=dec, note=note,
                     session=s["key"], hist=history(bars, s, today))
            rows.append(r)
        if not offline:
            time.sleep(pause)

    best = []
    for r in rows:
        if r.get("status") != "open":
            continue
        h = r["hist"][r["side"]]
        fresh = -1 < r["R"] <= CHASE_LIMIT_R
        if fresh and h["n"] >= MIN_HISTORY and h["avgR"] is not None and h["avgR"] > 0:
            best.append(r)
    best.sort(key=lambda r: (r["hist"][r["side"]]["avgR"]), reverse=True)

    waiting = []
    for r in rows:
        if r.get("status") != "waiting":
            continue
        cand = [r["hist"][s] for s in (1, -1) if r["hist"][s]["n"] >= MIN_HISTORY]
        if not cand:
            continue
        r["best_hist"] = max(h["avgR"] for h in cand)
        if r["best_hist"] > 0:
            waiting.append(r)
    waiting.sort(key=lambda r: r["best_hist"], reverse=True)
    return rows, best, waiting, errors


# --------------------------------------------------------------------------
# Webseite
# --------------------------------------------------------------------------

STATUS = {
    "pending": "Noch nicht gestartet",
    "forming": "Spanne bildet sich",
    "waiting": "Wartet auf Ausbruch",
    "open": "Signal aktiv",
    "target": "Ziel erreicht",
    "stop": "Stop erreicht",
    "expired": "Kein Ausbruch",
    "closed": "Session vorbei",
    "too_tight": "Spanne zu eng",
    "nodata": "Keine Daten / Markt zu",
    "error": "Abruf fehlgeschlagen",
}


def fmt(x, dec):
    if x is None:
        return "–"
    s = f"{x:,.{dec}f}"
    return s.replace(",", "'")


def pct(a, b):
    return f"{(a - b) / b * 100:+.2f} %"


def esc(s):
    return html.escape(str(s))


def hist_text(h):
    if not h["n"]:
        return "keine"
    return (f"{h['n']}× · Ziel {h['target']} · Stop {h['stop']} · "
            f"offen {h['neither']} · Ø {h['avgR']:+.2f} R")


def side_label(side):
    return "LONG ▲" if side > 0 else "SHORT ▼"


def card(r):
    d = r["dec"]
    s = next(x for x in SESSIONS if x["key"] == r["session"])
    h = r["hist"][r["side"]]
    cls = "long" if r["side"] > 0 else "short"
    return f"""
<article class="card {cls}">
  <header><span class="side">{side_label(r['side'])}</span>
  <h3>{esc(r['name'])}</h3><span class="muted">{esc(s['name'])} · seit {ch_time(r['trig_ts'])}</span></header>
  <dl>
    <div><dt>Einstieg</dt><dd>{fmt(r['entry'], d)}</dd></div>
    <div><dt>Stop</dt><dd>{fmt(r['stop'], d)} <small>{pct(r['stop'], r['entry'])}</small></dd></div>
    <div><dt>Ziel ({TARGET_R:g} R)</dt><dd>{fmt(r['target'], d)} <small>{pct(r['target'], r['entry'])}</small></dd></div>
    <div><dt>Jetzt</dt><dd>{fmt(r['price'], d)} <small>{r['R']:+.2f} R</small></dd></div>
  </dl>
  <p class="hist">Gleiche Regel, letzte ~60 Tage: {hist_text(h)}</p>
  {f'<p class="note">{esc(r["note"])}</p>' if r['note'] else ''}
</article>"""


def row_html(r):
    d = r["dec"]
    st = r.get("status", "error")
    label = STATUS.get(st, st)
    extra = ""
    if st == "pending":
        extra = f"Start {ch_time(r['t0'])}"
    elif st == "forming":
        extra = f"fertig {ch_time(r['t_or'])}"
    elif st == "waiting":
        extra = (f"Hoch {pct(r['hi'], r['last'])} · Tief {pct(r['lo'], r['last'])} "
                 f"· bis {ch_time(r['t_last'])}")
    elif st in ("open", "target", "stop", "closed"):
        label = f"{side_label(r['side'])} {label}"
        extra = f"seit {ch_time(r['trig_ts'])}"
        if "R" in r:
            extra += f" · {r['R']:+.2f} R"
    rng = (f"{fmt(r['lo'], d)} – {fmt(r['hi'], d)}" if "hi" in r else "–")
    lvl = "–"
    if "target" in r:
        lvl = (f"E {fmt(r['entry'], d)}<br>S {fmt(r['stop'], d)}<br>"
               f"Z {fmt(r['target'], d)}")
    h = r.get("hist") or {1: dict(n=0), -1: dict(n=0)}

    def hs(side):
        x = h[side]
        if not x["n"]:
            return "–"
        return f"{x['n']}× Ø {x['avgR']:+.2f} R"

    return (f"<tr class='st-{st}'><td><b>{esc(r['name'])}</b><br>"
            f"<small class='muted'>{esc(r['group'])}</small></td>"
            f"<td>{esc(label)}<br><small class='muted'>{esc(extra)}</small></td>"
            f"<td>{rng}</td><td>{lvl}</td>"
            f"<td>{hs(1)}</td><td>{hs(-1)}</td></tr>")


def render(rows, best, waiting, errors, now):
    stamp = local(now, "ch").strftime("%d.%m.%Y %H:%M")
    if best:
        best_html = "<div class='cards'>" + "".join(card(r) for r in best[:6]) + "</div>"
    else:
        best_html = ("<p class='empty'>Gerade kein frisches Signal, dessen Regel in den "
                     "letzten ~60 Tagen im Schnitt positiv war.</p>")
    if waiting:
        wl = "".join(
            f"<li><b>{esc(r['name'])}</b> ({esc(next(s['name'] for s in SESSIONS if s['key'] == r['session']))}): "
            f"Spanne {fmt(r['lo'], r['dec'])} – {fmt(r['hi'], r['dec'])}, jetzt {fmt(r['last'], r['dec'])}"
            f" <span class='muted'>· Historie bestes Ø {r['best_hist']:+.2f} R</span></li>"
            for r in waiting[:8])
        wait_html = f"<ul class='wait'>{wl}</ul>"
    else:
        wait_html = "<p class='empty'>Keine.</p>"

    tables = ""
    for s in SESSIONS:
        rs = [r for r in rows if r["session"] == s["key"]]
        t0 = next((r["t0"] for r in rs if "t0" in r), None)
        start = ch_time(t0) if t0 else ""
        tables += f"""
<section>
<h2>{esc(s['name'])}-Session <span class="muted">Start {start} Schweizer Zeit</span></h2>
<div class="scroll"><table>
<thead><tr><th>Markt</th><th>Status</th><th>Spanne (15 Min.)</th><th>Einstieg / Stop / Ziel</th>
<th>Historie Long</th><th>Historie Short</th></tr></thead>
<tbody>{''.join(row_html(r) for r in rs)}</tbody></table></div>
</section>"""

    err_html = ""
    if errors:
        err_html = ("<p class='note'>Nicht abrufbar: " +
                    esc("; ".join(e.split(":")[0] for e in errors)) + "</p>")

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>Breakout-Dashboard</title>
<style>
:root {{ --bg:#f6f5f2; --panel:#ffffff; --text:#1d1d1b; --muted:#6b6a66; --line:#e3e1dc;
  --long:#1f7a4d; --long-bg:#e6f3ec; --short:#b4352b; --short-bg:#f8e9e7; --accent:#2c5d8f; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#141413; --panel:#1f1f1d; --text:#ecebe6; --muted:#9c9a93; --line:#33322f;
  --long:#5cc08a; --long-bg:#18301f; --short:#ec7b70; --short-bg:#3a1d1a; --accent:#7fb0e0; }} }}
:root[data-theme="dark"] {{ --bg:#141413; --panel:#1f1f1d; --text:#ecebe6; --muted:#9c9a93; --line:#33322f;
  --long:#5cc08a; --long-bg:#18301f; --short:#ec7b70; --short-bg:#3a1d1a; --accent:#7fb0e0; }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:20px 16px 48px }}
h1 {{ font-size:22px; margin:0 0 4px }} h2 {{ font-size:17px; margin:28px 0 10px }}
.muted {{ color:var(--muted); font-weight:400 }} small {{ font-size:12px }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:12px }}
.card {{ background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--accent);
  border-radius:10px; padding:12px 14px }}
.card.long {{ border-left-color:var(--long) }} .card.short {{ border-left-color:var(--short) }}
.card h3 {{ margin:2px 0; font-size:17px }} .card header .muted {{ font-size:13px }}
.side {{ font-size:12px; font-weight:700; letter-spacing:.04em }}
.long .side {{ color:var(--long) }} .short .side {{ color:var(--short) }}
dl {{ display:grid; grid-template-columns:1fr 1fr; gap:6px 12px; margin:10px 0 }}
dt {{ font-size:12px; color:var(--muted) }} dd {{ margin:0; font-variant-numeric:tabular-nums; font-weight:600 }}
dd small {{ font-weight:400; color:var(--muted); white-space:nowrap }}
.hist {{ font-size:12px; color:var(--muted); margin:6px 0 0 }}
.note {{ font-size:12px; color:var(--muted); margin:4px 0 0 }}
.empty {{ background:var(--panel); border:1px dashed var(--line); border-radius:10px; padding:12px }}
.wait {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 10px 10px 30px; margin:0 }}
.wait li {{ margin:3px 0 }}
.scroll {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:10px }}
table {{ border-collapse:collapse; width:100%; min-width:760px; font-variant-numeric:tabular-nums }}
th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; font-size:14px }}
th {{ font-size:12px; color:var(--muted); font-weight:600 }}
tr.st-open td {{ background:var(--long-bg) }}
tr.st-stop td:nth-child(2) {{ color:var(--short) }} tr.st-target td:nth-child(2) {{ color:var(--long) }}
.disclaimer {{ margin-top:32px; font-size:13px; color:var(--muted); border-top:1px solid var(--line); padding-top:12px }}
</style></head>
<body><main>
<h1>Breakout-Dashboard</h1>
<p class="muted">Stand {stamp} Schweizer Zeit · aktualisiert sich alle 5 Minuten (die Seite lädt alle 2 Minuten neu)</p>

<h2>Beste frische Signale</h2>
<p class="muted">Signal aktiv, Kurs höchstens {CHASE_LIMIT_R:g} R vom Einstieg entfernt, und dieselbe Regel war beim
gleichen Markt und in der gleichen Session in den letzten ~60 Tagen im Schnitt positiv (mind. {MIN_HISTORY} Fälle).
Sortiert nach diesem Durchschnitt.</p>
{best_html}

<h2>Warten auf Ausbruch</h2>
{wait_html}

{tables}
{err_html}

<div class="disclaimer">
<p><b>So liest du das:</b> Die Eröffnungsspanne sind die ersten 15 Minuten der Session. Ein Signal entsteht, wenn eine
5-Minuten-Kerze <b>über</b> der Spanne schliesst (Long) oder <b>darunter</b> (Short). Stop = Mitte der Spanne,
Ziel = {TARGET_R:g}× das Risiko. „R“ ist das Vielfache des Risikos: +{TARGET_R:g} R = Ziel, −1 R = Stop.
„Historie“ zeigt, wie dieselbe Regel in den letzten ~60 Tagen ausgegangen ist (Anzahl, Ziel, Stop, offen bei
Session-Ende, Durchschnitt in R).</p>
<p><b>Keine Anlageberatung.</b> Kostenlose, verzögerte Yahoo-Daten. Futures und Kassakurse weichen von den CFD-Kursen
deines Brokers ab: Übernimm Stop und Ziel als Prozent-Abstand, nicht als feste Zahl. CFDs und Krypto haben oft einen
Hebel, damit kannst du mehr verlieren als hier berechnet. Die Historie ist kein Versprechen für die Zukunft.
Die meisten Daytrader verlieren Geld.</p>
</div>
</main></body></html>
"""


def to_json(rows, best, now):
    def clean(r):
        r = dict(r)
        if "hist" in r:
            r["hist"] = {"long": r["hist"][1], "short": r["hist"][-1]}
        return r
    return dict(generated=datetime.fromtimestamp(now, UTC).isoformat(),
                best=[f"{r['symbol']}:{r['session']}" for r in best],
                rows=[clean(r) for r in rows])


def parse_now(s):
    if not s:
        return int(time.time())
    t = datetime.strptime(s, "%Y-%m-%d %H:%M")
    return local_to_ts(t.date(), t.hour, t.minute, "ch")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="site")
    ap.add_argument("--now", help='Schweizer Zeit, z. B. "2026-10-22 16:10"')
    ap.add_argument("--offline", help="Ordner mit gespeicherten Yahoo-JSONs")
    a = ap.parse_args(argv)
    now = parse_now(a.now)
    rows, best, waiting, errors = evaluate(now, a.offline)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(rows, best, waiting, errors, now))
    with open(os.path.join(a.out, "data.json"), "w", encoding="utf-8") as f:
        json.dump(to_json(rows, best, now), f, ensure_ascii=False, indent=1)
    ok = len(MARKETS) - len(errors)
    print(f"{ok} von {len(MARKETS)} Maerkten geladen, {len(best)} frische Signale, "
          f"{len(waiting)} warten. -> {os.path.join(a.out, 'index.html')}")
    for e in errors:
        print("  Fehler:", e, file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
