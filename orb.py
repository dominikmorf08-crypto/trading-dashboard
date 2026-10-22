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
import bisect
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
MIN_RISK_PCT_FX = 0.0002  # Devisen: 0,02 % (rund 2 Pips bei EUR/USD)
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


def run_session(bars, d, s, now=None, live=None, min_risk=MIN_RISK_PCT):
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
    if risk < entry * min_risk:
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


def history(bars, s, today, min_risk=MIN_RISK_PCT):
    days = sorted({local(b[0], s["tz"]).date() for b in bars})
    out = {1: [], -1: []}
    timing = []   # Minuten vom Ende der Spanne bis zum Ausbruch, None = kein Ausbruch
    for d in days:
        if d >= today:
            continue
        r = run_session(bars, d, s, min_risk=min_risk)
        if r.get("status") in ("stop", "target", "closed", "open"):
            out[r["side"]].append(r["R"])
        if "trig_ts" in r:
            timing.append((r["trig_ts"] - r["t_or"]) / 60)
        elif r.get("status") == "expired":
            timing.append(None)
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
    stats["timing"] = timing
    return stats


def _q(xs, f):
    xs = sorted(xs)
    i = (len(xs) - 1) * f
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def forecast(r, now):
    """Wann kommt der Ausbruch? Aus den vergangenen ~60 Tagen desselben Markts und derselben Session."""
    timing = (r.get("hist") or {}).get("timing") or []
    if len(timing) < MIN_HISTORY or "t_or" not in r:
        return None
    el = max(0.0, (now - r["t_or"]) / 60)
    base = [m for m in timing if m is None or m > el]
    if len(base) < 5:
        return None
    later = [m - el for m in base if m is not None]
    f = dict(p=len(later) / len(base), n=len(base), elapsed=el)
    if later:
        f.update(med=_q(later, .5), q1=_q(later, .25), q3=_q(later, .75))
        f["eta"] = int(max(now, r["t_or"]) + f["med"] * 60)
    return f


# --------------------------------------------------------------------------
# Auswertung
# --------------------------------------------------------------------------


TM_REPLAYS = 2000
TM_SEED = 19
TM_WINDOW = 2     # Kerzen aus +/- 2 Zeitfenstern derselben Tageszeit
TM_MIN_DAYS = 20


def timemachine(bars, s, r, today, replays=TM_REPLAYS, seed=TM_SEED):
    """Spielt den Rest der Session tausendfach nach, Kerze fuer Kerze aus den eigenen
    vergangenen Kerzen derselben Tageszeit (wie trader-timemachine)."""
    import random
    nbar = (r["t_end"] - r["t0"]) // BAR
    by_ts = {b[0]: b for b in bars}
    pool = [[] for _ in range(nbar)]
    days = 0
    for d in sorted({local(b[0], s["tz"]).date() for b in bars}):
        if d >= today:
            continue
        t0 = local_to_ts(d, *s["start"], s["tz"])
        seq = [by_ts.get(t0 + i * BAR) for i in range(nbar)]
        if any(x is None for x in seq):
            continue
        prev = by_ts.get(t0 - BAR)
        prev_c = prev[4] if prev else seq[0][1]
        days += 1
        for i, b in enumerate(seq):
            pool[i].append((b[1] / prev_c, b[2] / prev_c, b[3] / prev_c, b[4] / prev_c))
            prev_c = b[4]
    if days < TM_MIN_DAYS:
        return None
    start = (r["trig_ts"] - r["t0"]) // BAR          # erste Kerze nach dem Einstieg
    if start >= nbar:
        return None
    win = []
    for i in range(nbar):
        w = []
        for j in range(max(0, i - TM_WINDOW), min(nbar, i + TM_WINDOW + 1)):
            w.extend(pool[j])
        win.append(w)
    rnd = random.Random(seed)
    side, entry, stop, target, risk = r["side"], r["entry"], r["stop"], r["target"], r["risk"]
    tp = sl = 0
    rs = []
    for _ in range(replays):
        p = entry
        out = None
        for i in range(start, nbar):
            o, h, l, c = rnd.choice(win[i])
            hi, lo = p * h, p * l
            if (lo <= stop) if side > 0 else (hi >= stop):
                out = -1.0
                break
            if (hi >= target) if side > 0 else (lo <= target):
                out = TARGET_R
                break
            p *= c
        if out == -1.0:
            sl += 1
        elif out is not None:
            tp += 1
        rs.append(out if out is not None else side * (p - entry) / risk)
    n = replays
    res = dict(n=n, days=days, seed=seed, bars=nbar - start, tp=tp / n, sl=sl / n,
               open=(n - tp - sl) / n, avgR=sum(rs) / n)
    hit = tp + sl
    if hit:
        edge = (tp / hit - BREAKEVEN) * 100
        side_w = "TP" if edge > 0 else "SL"
        if abs(edge) < 5:
            res["verdict"], res["vcls"] = "Münzwurf", "q-flip"
        elif abs(edge) < 15:
            res["verdict"], res["vcls"] = f"leicht Richtung {side_w}", "q-good" if edge > 0 else "q-weak"
        else:
            res["verdict"], res["vcls"] = f"deutlich Richtung {side_w}", "q-strong" if edge > 0 else "q-weak"
    else:
        res["verdict"], res["vcls"] = "bleibt meist offen", "q-flip"
    return res


def tm_html(tm):
    if not tm:
        return ""
    p = lambda x: f"{round(x * 100)} %"
    return (f"<div class='tm {tm['vcls']}'><div class='tm-h'><b>Zeitmaschine</b>"
            f"<span>{tm['n']:,} Durchläufe</span></div>".replace(",", "'") +
            f"<div class='tm-v'>{esc(tm['verdict'])}</div>"
            f"<div class='meter3'><span class='m-tp' style='width:{tm['tp'] * 100:.1f}%'></span>"
            f"<span class='m-op' style='width:{tm['open'] * 100:.1f}%'></span>"
            f"<span class='m-sl' style='width:{tm['sl'] * 100:.1f}%'></span>"
            f"<i style='left:{BREAKEVEN * (tm['tp'] + tm['sl']) * 100:.1f}%'></i></div>"
            f"<div class='qfoot'><span class='k-tp'>TP zuerst {p(tm['tp'])}</span>"
            f"<span class='k-sl'>SL zuerst {p(tm['sl'])}</span><span>offen bis Schluss {p(tm['open'])}</span>"
            f"<span>Ø {tm['avgR']:+.2f} R</span></div>"
            f"<p class='hist'>Ab dem Einstieg den Rest der Session ({tm['bars']} Kerzen) {tm['n']}-mal nachgespielt aus den eigenen "
            f"5-Minuten-Kerzen von {tm['days']} vergangenen Tagen, Seed {tm['seed']}. Simulation, keine Vorhersage.</p></div>")


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
        mr0 = MIN_RISK_PCT_FX if group == "Devisen" else MIN_RISK_PCT
        BT[sym] = bt_trades(bars, sym, group, local(now, "us").date(), mr0)
        for s in SESSIONS:
            if sym in LONDON_ONLY and s["key"] != "london":
                continue
            today = local(now, s["tz"]).date()
            mr = MIN_RISK_PCT_FX if group == "Devisen" else MIN_RISK_PCT
            r = run_session(bars, today, s, now=now, live=live, min_risk=mr)
            r.update(symbol=sym, name=name, group=group, dec=dec, note=note,
                     session=s["key"], hist=history(bars, s, today, mr))
            if r.get("status") in ("pending", "forming", "waiting"):
                r["fc"] = forecast(r, now)
            if r.get("status") in ("open", "target", "stop", "closed"):
                r["tm"] = timemachine(bars, s, r, today)
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
        waiting.append(r)
    waiting.sort(key=lambda r: (r["best_hist"] > 0, (r.get("fc") or {}).get("p", 0), r["best_hist"]),
                 reverse=True)
    return rows, best, waiting, errors


# --------------------------------------------------------------------------
# Webseite
# --------------------------------------------------------------------------

STATUS = {
    "pending": "Noch nicht gestartet",
    "forming": "Spanne bildet sich",
    "waiting": "Wartet auf Ausbruch",
    "open": "Signal aktiv",
    "target": "TP erreicht",
    "stop": "SL erreicht",
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


BREAKEVEN = 1 / (1 + TARGET_R)   # 2 R: 33 % Treffer reichen zum Ausgleich


def quality(h):
    """Trefferquote in % und eine Einstufung wie die Zeitmaschine der Skills."""
    if not h or not h["n"]:
        return None, "zu wenig Daten", "q-none"
    win = h["target"] / h["n"]
    if h["n"] < MIN_HISTORY:
        return win, "zu wenig Daten", "q-none"
    edge = (win - BREAKEVEN) * 100
    if edge >= 15:
        return win, "stark", "q-strong"
    if edge >= 5:
        return win, "gut", "q-good"
    if edge > -5:
        return win, "Münzwurf", "q-flip"
    return win, "schwach", "q-weak"


def hist_text(h):
    if not h["n"]:
        return "Noch keine vergangenen Fälle."
    return (f"Letzte ~60 Tage: {h['n']} gleiche Signale, davon {h['target']}× TP, {h['stop']}× SL, "
            f"{h['neither']}× bis Session-Ende offen. Im Schnitt {h['avgR']:+.2f} R.")


def side_word(side):
    return "Long" if side > 0 else "Short"


def side_arrow(side):
    return "↗" if side > 0 else "↘"


def sess(key):
    return next(x for x in SESSIONS if x["key"] == key)


def quality_html(h, big=True):
    win, label, cls = quality(h)
    be = round(BREAKEVEN * 100)
    if win is None or label == "zu wenig Daten":
        return "<div class='q q-none'><span class='qlabel'>Zu wenig vergangene Fälle für eine TP-Quote</span></div>"
    w = round(win * 100)
    sl = round(h["stop"] / h["n"] * 100)
    op = 100 - w - sl
    return (f"<div class='q {cls}'><div class='qrow'><span class='qnum'>{w}<small>%</small></span>"
            f"<span class='qtext'>geht in den <b>TP</b><br>Stärke: <b>{label}</b></span></div>"
            f"<div class='meter3' role='img' aria-label='{w} Prozent TP, {sl} Prozent SL, Ausgleich bei {be} Prozent'>"
            f"<span class='m-tp' style='width:{w}%'></span><span class='m-op' style='width:{op}%'></span>"
            f"<span class='m-sl' style='width:{sl}%'></span><i style='left:{be}%'></i></div>"
            f"<div class='qfoot'><span class='k-tp'>TP {w} %</span><span class='k-sl'>SL {sl} %</span>"
            f"{f'<span>offen bis Schluss {op} %</span>' if op else ''}<span>Ausgleich ab {be} % TP</span></div></div>")


def strip(lo, hi, last, stop=None, entry=None, target=None, side=0, w=320, h=54):
    """Kleine Preisleiter: Eroeffnungsspanne (Band), Stop, Einstieg, Ziel, Kurs jetzt."""
    vals = [v for v in (lo, hi, last, stop, entry, target) if v is not None]
    a, b = min(vals), max(vals)
    pad = (b - a) * 0.06 or a * 0.001
    a, b = a - pad, b + pad

    def x(v):
        return 8 + (v - a) / (b - a) * (w - 16)

    y = 30
    parts = [f"<svg class='strip' viewBox='0 0 {w} {h}' aria-hidden='true'>",
             f"<line x1='8' y1='{y}' x2='{w - 8}' y2='{y}' class='axis'/>",
             f"<rect x='{x(lo):.1f}' y='{y - 11}' width='{max(x(hi) - x(lo), 2):.1f}' height='22' rx='3' class='band'/>"]
    if entry is not None:
        good = side * (last - entry) >= 0
        x1, x2 = sorted((x(entry), x(last)))
        parts.append(f"<rect x='{x1:.1f}' y='{y - 3}' width='{max(x2 - x1, 1):.1f}' height='6' rx='3' "
                     f"class='{'run-up' if good else 'run-down'}'/>")
        for v, cls, t in ((stop, "tick-stop", "SL"), (entry, "tick-entry", "Einstieg"),
                          (target, "tick-target", "TP")):
            parts.append(f"<line x1='{x(v):.1f}' y1='{y - 16}' x2='{x(v):.1f}' y2='{y + 16}' class='{cls}'/>"
                         f"<text x='{x(v):.1f}' y='{y - 20}' class='lab {cls}'>{t}</text>")
    parts.append(f"<circle cx='{x(last):.1f}' cy='{y}' r='5.5' class='now'/>")
    parts.append("</svg>")
    return "".join(parts)


def card(r):
    d = r["dec"]
    s = sess(r["session"])
    h = r["hist"][r["side"]]
    cls = "long" if r["side"] > 0 else "short"
    note = f"<p class='note'>{esc(r['note'])}.</p>" if r["note"] else ""
    return f"""
<article class="sig {cls}">
  <header>
    <div><h3>{esc(r['name'])}</h3>
    <p class="meta">{esc(s['name'].replace(' ', '-'))}-Session, ausgelöst {ch_time(r['trig_ts'])}</p></div>
    <span class="dir">{side_arrow(r['side'])} {side_word(r['side'])}</span>
  </header>
  {strip(r['lo'], r['hi'], r['price'], r['stop'], r['entry'], r['target'], r['side'])}
  <dl class="lv">
    <div><dt>SL</dt><dd>{fmt(r['stop'], d)}</dd><dd class="sub">{pct(r['stop'], r['entry'])}</dd></div>
    <div><dt>Einstieg</dt><dd>{fmt(r['entry'], d)}</dd><dd class="sub">&nbsp;</dd></div>
    <div><dt>TP</dt><dd>{fmt(r['target'], d)}</dd><dd class="sub">{pct(r['target'], r['entry'])}</dd></div>
    <div><dt>Jetzt</dt><dd>{fmt(r['price'], d)}</dd><dd class="sub">{r['R']:+.2f} R</dd></div>
  </dl>
  {quality_html(h)}
  <p class="hist">{hist_text(h)}</p>
  {tm_html(r.get('tm'))}
  {note}
</article>"""


def dur(mins):
    mins = max(0, int(round(mins)))
    if mins < 60:
        return f"{mins} Min."
    h, m = divmod(mins, 60)
    return f"{h} Std. {m} Min." if m else f"{h} Std."


def fc_short(r):
    f = r.get("fc")
    if not f:
        return ""
    if "med" not in f:
        return f"bisher meist kein Ausbruch mehr ({round(f['p'] * 100)} %)"
    if r.get("status") in ("pending", "forming"):
        return f"Ausbruch meist um {ch_time(f['eta'])} ({round(f['p'] * 100)} % der Tage)"
    return f"Ausbruch meist in ~{dur(f['med'])} ({round(f['p'] * 100)} %)"


def fc_long(r):
    f = r.get("fc")
    if not f:
        return "<p class='fc none'>Für eine Zeitschätzung gibt es zu wenig vergangene Tage.</p>"
    p = round(f["p"] * 100)
    if "med" not in f:
        return (f"<p class='fc'><b>Eher kein Ausbruch mehr.</b> An den {f['n']} vergleichbaren Tagen kam um diese "
                f"Zeit nur noch in {p} % ein Ausbruch.</p>")
    return (f"<p class='fc'><span class='eta' data-at='{f['eta']}'>in ~{dur(f['med'])}</span>"
            f"<span>Ausbruch geschätzt um <b>{ch_time(f['eta'])}</b>, meist zwischen {dur(f['q1'])} und "
            f"{dur(f['q3'])} von jetzt. Kam bisher an {p} % der {f['n']} vergleichbaren Tage.</span></p>")


def zones(rows, now):
    """Countdown: wann oeffnet bzw. schliesst die Einstiegszone jeder Session."""
    out = ""
    for s in SESSIONS:
        r = next((x for x in rows if x["session"] == s["key"] and "t0" in x), None)
        if not r:
            continue
        name = esc(s["name"])
        if now < r["t_or"]:
            out += (f"<div class='zone soon'><span>{name}</span><b data-until='{r['t_or']}'>in "
                    f"{dur((r['t_or'] - now) / 60)}</b><small>Einstiegszone öffnet um {ch_time(r['t_or'])}</small></div>")
        elif now < r["t_last"]:
            out += (f"<div class='zone open'><span>{name}</span><b>offen</b><small>Einstiege bis "
                    f"{ch_time(r['t_last'])}, noch <em data-until='{r['t_last']}'>{dur((r['t_last'] - now) / 60)}</em>"
                    f"</small></div>")
        else:
            out += (f"<div class='zone done'><span>{name}</span><b>vorbei</b><small>keine neuen Einstiege "
                    f"mehr heute</small></div>")
    return f"<div class='zones'>{out}</div>"


def status_cell(r):
    st = r.get("status", "error")
    label = STATUS.get(st, st)
    extra, dot = "", "idle"
    if st == "pending":
        extra = f"startet {ch_time(r['t0'])}. {fc_short(r)}".strip(". ")
    elif st == "forming":
        extra, dot = f"steht um {ch_time(r['t_or'])}. {fc_short(r)}".strip(". "), "wait"
    elif st == "waiting":
        extra, dot = f"jetzt {fmt(r['last'], r['dec'])}. {fc_short(r)}".strip(". "), "wait"
    elif st in ("open", "target", "stop", "closed"):
        label = f"{side_word(r['side'])}: {label}"
        extra = f"seit {ch_time(r['trig_ts'])}"
        if "R" in r:
            extra += f", {r['R']:+.2f} R"
        dot = {"open": "live", "target": "win", "stop": "loss"}.get(st, "idle")
    return (f"<span class='st'><i class='dot {dot}'></i>{esc(label)}</span>"
            f"<small>{esc(extra)}</small>")


def qcell(h):
    if not h or not h["n"]:
        return "<span class='qc q-none'>–</span>"
    win, label, cls = quality(h)
    return (f"<span class='qc {cls}'><b>{round(win * 100)} % TP</b> {label}</span>"
            f"<small>SL {round(h['stop'] / h['n'] * 100)} %, {h['n']} Fälle</small>")


def row_html(r):
    d = r["dec"]
    rng = f"{fmt(r['lo'], d)} – {fmt(r['hi'], d)}" if "hi" in r else "–"
    lvl = "–"
    if "target" in r:
        lvl = (f"<span class='lv3'><span>SL {fmt(r['stop'], d)}</span><span>E {fmt(r['entry'], d)}</span>"
               f"<span>TP {fmt(r['target'], d)}</span></span>")
    h = r.get("hist") or {1: dict(n=0), -1: dict(n=0)}
    return (f"<tr class='st-{r.get('status', 'error')}'><th scope='row'>{esc(r['name'])}</th>"
            f"<td data-l='Status'>{status_cell(r)}</td>"
            f"<td class='num{' e' if rng == '–' else ''}' data-l='Spanne'>{rng}</td>"
            f"<td class='num lvc{' e' if lvl == '–' else ''}' data-l='SL, Einstieg, TP'>{lvl}</td>"
            f"<td data-l='Long: TP-Quote'>{qcell(h[1])}</td><td data-l='Short: TP-Quote'>{qcell(h[-1])}</td></tr>")


def timeline(rows, now):
    """Tagesleiste 07:00-23:00 Schweizer Zeit mit beiden Sessions und der Uhrzeit jetzt."""
    day0 = local(now, "ch").replace(hour=7, minute=0, second=0, microsecond=0)
    span = 16 * 60

    def pos(ts):
        m = (local(ts, "ch") - day0).total_seconds() / 60
        return max(0.0, min(100.0, m / span * 100))

    bands = ""
    for s in SESSIONS:
        r = next((x for x in rows if x["session"] == s["key"] and "t0" in x), None)
        if not r:
            continue
        a, o, b = pos(r["t0"]), pos(r["t_or"]), pos(r["t_end"])
        bands += (f"<div class='band-s {s['key']}' style='left:{a:.2f}%;width:{b - a:.2f}%'>"
                  f"<span class='orw' style='width:{(o - a) / (b - a) * 100:.2f}%'></span>"
                  f"<em>{esc(s['name'])} {ch_time(r['t0'])}–{ch_time(r['t_end'])}</em></div>")
    ticks = "".join(f"<span style='left:{(hh - 7) / 16 * 100:.2f}%'>{hh}</span>" for hh in range(8, 23, 2))
    return (f"<div class='tl' aria-label='Handelstag in Schweizer Zeit'><div class='tl-track'>{bands}"
            f"<div class='tl-now' style='left:{pos(now):.2f}%'><b>{local(now, 'ch').strftime('%H:%M')}</b></div>"
            f"</div><div class='tl-ticks'>{ticks}</div></div>")


def wr(r, side):
    win, label, _ = quality(r["hist"][side])
    return "–" if win is None else f"{round(win * 100)} % TP ({label})"


def pick_calls(rows):
    """Calls: heutige Signale mit Staerke 'stark', bei denen die Zeitmaschine nicht Richtung SL zeigt."""
    out = []
    for r in rows:
        if r.get("status") not in ("open", "target", "stop", "closed"):
            continue
        win, label, _ = quality(r["hist"][r["side"]])
        tm = r.get("tm")
        if label != "stark" or (tm and "SL" in tm["verdict"]):
            continue
        out.append((win, r))
    out.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in out]


def render_calls(calls, rows, now):
    if not calls:
        nxt = ""
        for s in SESSIONS:
            r = next((x for x in rows if x["session"] == s["key"] and "t0" in x), None)
            if r and now < r["t_last"]:
                nxt = (f" Die nächsten Calls kommen frühestens um <b>{ch_time(r['t_or'])}</b> "
                       f"({esc(s['name'])}-Session)." if now < r["t_or"] else
                       f" Die {esc(s['name'])}-Session läuft noch bis {ch_time(r['t_last'])}, "
                       f"neue Calls kommen hier sofort dazu.")
                break
        return (f"<section class='calls' id='calls'><div class='sec'><h2>Calls heute</h2></div>"
                f"<p class='empty'>Heute noch kein Signal, das stark genug ist.{nxt}</p></section>")
    n_tp = sum(1 for r in calls if r["status"] == "target")
    n_sl = sum(1 for r in calls if r["status"] == "stop")
    n_run = sum(1 for r in calls if r["status"] == "open")
    items = ""
    for r in calls:
        d = r["dec"]
        win, label, _ = quality(r["hist"][r["side"]])
        tm = r.get("tm")
        st = r["status"]
        if st == "open":
            state, scls = f"läuft, {r['R']:+.2f} R", "run"
        elif st == "target":
            state, scls = f"TP erreicht um {ch_time(r['exit_ts'])}, +{TARGET_R:g} R", "won"
        elif st == "stop":
            state, scls = f"SL erreicht um {ch_time(r['exit_ts'])}, −1 R", "lost"
        else:
            state, scls = f"Session-Ende, {r['R']:+.2f} R", "done"
        tmtxt = f"Zeitmaschine: {esc(tm['verdict'])}, TP zuerst {round(tm['tp'] * 100)} %" if tm else ""
        items += f"""
<li class="call {'long' if r['side'] > 0 else 'short'} {scls}">
  <div class="c-time">{ch_time(r['trig_ts'])}<small>{esc(sess(r['session'])['name'])}</small></div>
  <div class="c-main"><b>{esc(r['name'])}</b><span class="dir">{side_arrow(r['side'])} {side_word(r['side'])}</span></div>
  <div class="c-lv"><span>Einstieg <b>{fmt(r['entry'], d)}</b></span><span>SL <b>{fmt(r['stop'], d)}</b> <small>{pct(r['stop'], r['entry'])}</small></span><span>TP <b>{fmt(r['target'], d)}</b> <small>{pct(r['target'], r['entry'])}</small></span></div>
  <div class="c-q"><span class="qnum">{round(win * 100)}<small>%</small></span><span>TP-Quote<br>{tmtxt}</span></div>
  <div class="c-st">{state}</div>
</li>"""
    return f"""
<section class="calls" id="calls">
<div class="sec"><h2>Calls heute</h2>
<p>{len(calls)} Calls: {n_tp}× TP, {n_sl}× SL, {n_run} laufen noch. Stärkste zuerst. Ein Call entsteht, sobald ein
Signal mit Stärke „stark“ auslöst und die Zeitmaschine nicht Richtung SL zeigt.</p></div>
<ul class="calllist">{items}</ul>
</section>"""


def render(rows, best, waiting, errors, now):
    stamp = local(now, "ch")
    days = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    when = f"{days[stamp.weekday()]}, {stamp.strftime('%d.%m.%Y')}, Stand {stamp.strftime('%H:%M')} Uhr"
    n_open = sum(1 for r in rows if r.get("status") == "open")

    if best:
        best_html = "<div class='sigs'>" + "".join(card(r) for r in best[:6]) + "</div>"
    else:
        best_html = ("<p class='empty'>Gerade gibt es kein frisches Signal mit guter Vorgeschichte. "
                     "Die Seite prüft alle 5 Minuten neu, ab 09:15 (London) und 15:45 (New York) "
                     "kommen neue Signale.</p>")

    if waiting:
        items = ""
        for r in waiting[:10]:
            items += (f"<li><div class='w-name'><b>{esc(r['name'])}</b>"
                      f"<span>{esc(sess(r['session'])['name'])}, jetzt {fmt(r['last'], r['dec'])}</span></div>"
                      f"{strip(r['lo'], r['hi'], r['last'], w=240, h=40)}"
                      f"<div class='w-d'><span>bis oben <b>{pct(r['hi'], r['last'])}</b></span>"
                      f"<span>bis unten <b>{pct(r['lo'], r['last'])}</b></span></div>"
                      f"<div class='w-q'><span>Long {wr(r, 1)}</span><span>Short {wr(r, -1)}</span></div>"
                      f"{fc_long(r)}</li>")
        wait_html = f"<ul class='waits'>{items}</ul>"
    else:
        wait_html = "<p class='empty'>Gerade wartet kein Markt auf einen Ausbruch. Oben steht, wann die nächste Einstiegszone öffnet.</p>"

    tables = ""
    for s in SESSIONS:
        rs = [r for r in rows if r["session"] == s["key"]]
        live = any(r.get("status") in ("pending", "forming", "waiting", "open") for r in rs)
        t0 = next((r["t0"] for r in rs if "t0" in r), None)
        body = ""
        for g in dict.fromkeys(r["group"] for r in rs):
            body += f"<tr class='grp'><th colspan='6' scope='rowgroup'>{esc(g)}</th></tr>"
            body += "".join(row_html(r) for r in rs if r["group"] == g)
        tables += f"""
<details class="sess" {'open' if live else ''}>
<summary><h2>{esc(s['name'].replace(' ', '-'))}-Session</h2><span>Start {ch_time(t0) if t0 else ''} Uhr, {len(rs)} Märkte</span></summary>
<div class="scroll"><table>
<thead><tr><th scope="col">Markt</th><th scope="col">Status</th><th scope="col">Spanne</th>
<th scope="col">SL, Einstieg, TP</th><th scope="col">Long: TP-Quote</th><th scope="col">Short: TP-Quote</th></tr></thead>
<tbody>{body}</tbody></table></div>
</details>"""

    err_html = ""
    if errors:
        err_html = ("<p class='empty'>Diese Märkte konnten nicht geladen werden und fehlen: " +
                    esc(", ".join(e.split(":")[0] for e in errors)) + ". Beim nächsten Lauf wird es neu versucht.</p>")

    be = round(BREAKEVEN * 100)
    sim = bot_sim()
    bot_html = render_bot(sim, bot_today(rows, sim["eq"]))
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120">
<title>Breakout-Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#EEF1F5; --surface:#FFFFFF; --ink:#15233B; --muted:#5D6B80; --line:#D5DCE6;
  --long:#0E8A6A; --long-soft:#DDF2EA; --short:#C8364B; --short-soft:#F8E1E5;
  --range:#E4A11B; --range-soft:rgba(228,161,27,.22); --flip:#7A8699;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#0E1625; --surface:#16223A; --ink:#E4EAF3; --muted:#8D9AB0; --line:#26344D;
  --long:#3DCB9E; --long-soft:#123A32; --short:#F0667A; --short-soft:#3F1C26;
  --range:#F2B544; --range-soft:rgba(242,181,68,.2); --flip:#8D9AB0; }} }}
:root[data-theme="dark"] {{ --bg:#0E1625; --surface:#16223A; --ink:#E4EAF3; --muted:#8D9AB0; --line:#26344D;
  --long:#3DCB9E; --long-soft:#123A32; --short:#F0667A; --short-soft:#3F1C26;
  --range:#F2B544; --range-soft:rgba(242,181,68,.2); --flip:#8D9AB0; }}
* {{ box-sizing:border-box }}
html {{ -webkit-text-size-adjust:100% }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 Barlow,system-ui,sans-serif }}
main {{ max-width:1120px; margin:0 auto; padding:28px 16px 56px }}
h1,h2,h3,.qnum,.dir,.lv dd,.tl-now b {{ font-family:"Barlow Semi Condensed",Barlow,sans-serif }}
.num, .lv dd, .qnum, td small {{ font-variant-numeric:tabular-nums }}

.top {{ display:flex; flex-wrap:wrap; align-items:flex-end; justify-content:space-between; gap:8px 24px; margin-bottom:18px }}
h1 {{ font-size:44px; line-height:1; margin:0; font-weight:700; letter-spacing:-.01em }}
.top p {{ margin:0; color:var(--muted) }}
.count {{ font-family:"Barlow Semi Condensed",sans-serif; font-size:18px; color:var(--ink) }}
.count b {{ color:var(--long) }}
.btlink {{ font:500 15px Barlow,sans-serif; color:var(--ink); text-underline-offset:3px }}

.tl {{ margin:6px 0 34px }}
.tl-track {{ position:relative; height:46px; background:var(--surface); border:1px solid var(--line); border-radius:8px }}
.band-s {{ position:absolute; top:7px; height:14px; border-radius:4px; background:var(--line) }}
.band-s.ny {{ top:25px }}
.band-s .orw {{ position:absolute; left:0; top:0; bottom:0; background:var(--range); border-radius:4px 0 0 4px }}
.band-s em {{ position:absolute; left:6px; top:-1px; font-style:normal; font-size:11.5px; line-height:16px; color:var(--ink); white-space:nowrap; padding-left:8px }}
.tl-now {{ position:absolute; top:-6px; bottom:-6px; width:2px; background:var(--ink) }}
.tl-now b {{ position:absolute; top:-20px; left:50%; transform:translateX(-50%); font-size:13px; background:var(--ink); color:var(--bg); padding:0 5px; border-radius:3px }}
.tl-ticks {{ position:relative; height:18px; color:var(--muted); font-size:12px }}
.tl-ticks span {{ position:absolute; transform:translateX(-50%); top:2px }}

h2 {{ font-size:26px; margin:0; font-weight:600 }}
.sec {{ margin:0 0 12px }} .sec p {{ margin:2px 0 0; color:var(--muted); max-width:72ch }}
section {{ margin-bottom:38px }}

.sigs {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px }}
.sig {{ background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:16px 16px 14px; position:relative; overflow:hidden }}
.sig::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:var(--long) }}
.sig.short::before {{ background:var(--short) }}
.sig header {{ display:flex; justify-content:space-between; align-items:flex-start; gap:10px }}
.sig h3 {{ font-size:26px; line-height:1.05; margin:0; font-weight:700 }}
.meta {{ margin:2px 0 0; color:var(--muted); font-size:14px }}
.dir {{ font-size:17px; font-weight:700; padding:3px 10px; border-radius:6px; background:var(--long-soft); color:var(--long); white-space:nowrap }}
.short .dir {{ background:var(--short-soft); color:var(--short) }}

.strip {{ display:block; width:100%; height:auto; margin:10px 0 6px; overflow:visible }}
.strip .axis {{ stroke:var(--line); stroke-width:2 }}
.strip .band {{ fill:var(--range-soft); stroke:var(--range); stroke-width:1.5 }}
.strip .run-up {{ fill:var(--long) }} .strip .run-down {{ fill:var(--short) }}
.strip line.tick-stop {{ stroke:var(--short); stroke-width:2 }}
.strip line.tick-target {{ stroke:var(--long); stroke-width:2 }}
.strip line.tick-entry {{ stroke:var(--ink); stroke-width:2; stroke-dasharray:3 3 }}
.strip .lab {{ font:600 11px Barlow,sans-serif; text-anchor:middle }}
.strip text.tick-stop {{ fill:var(--short) }} .strip text.tick-target {{ fill:var(--long) }} .strip text.tick-entry {{ fill:var(--muted) }}
.strip .now {{ fill:var(--surface); stroke:var(--ink); stroke-width:2.5 }}

.lv {{ display:grid; grid-template-columns:repeat(4,1fr); gap:4px; margin:0 0 12px }}
.lv dt {{ font-size:12.5px; color:var(--muted) }}
.lv dd {{ margin:0; font-size:18px; font-weight:600; line-height:1.15; overflow-wrap:anywhere }}
.lv dd.sub {{ font:500 12.5px Barlow,sans-serif; color:var(--muted) }}

.q {{ border-top:1px solid var(--line); padding-top:10px }}
.qrow {{ display:flex; align-items:center; gap:10px }}
.qnum {{ font-size:40px; font-weight:700; line-height:1 }}
.qnum small {{ font-size:22px; margin-left:1px }}
.qtext {{ font-size:13.5px; line-height:1.25; color:var(--muted) }}
.qtext b {{ font-size:16px }}
.q-strong .qnum, .q-strong .qtext b {{ color:var(--long) }}
.q-good .qtext b {{ color:var(--long) }}
.q-weak .qnum, .q-weak .qtext b {{ color:var(--short) }}
.q-flip .qtext b {{ color:var(--flip) }}
.meter {{ position:relative; height:8px; background:var(--bg); border-radius:4px; margin:8px 0 3px }}
.meter span {{ position:absolute; left:0; top:0; bottom:0; border-radius:4px; background:var(--flip) }}
.q-strong .meter span, .q-good .meter span {{ background:var(--long) }}
.q-weak .meter span {{ background:var(--short) }}
.meter i {{ position:absolute; top:-4px; bottom:-4px; width:2px; background:var(--ink) }}
.meter3 {{ position:relative; display:flex; height:10px; background:var(--bg); border-radius:5px; margin:8px 0 4px; overflow:visible }}
.meter3 span {{ height:100% }}
.m-tp {{ background:var(--long); border-radius:5px 0 0 5px }} .m-sl {{ background:var(--short); border-radius:0 5px 5px 0 }}
.m-op {{ background:var(--line) }}
.meter3 i {{ position:absolute; top:-4px; bottom:-4px; width:2px; background:var(--ink) }}
.qfoot {{ display:flex; flex-wrap:wrap; gap:2px 12px; font-size:12.5px; color:var(--muted) }}
.tm {{ border-top:1px solid var(--line); margin-top:10px; padding-top:10px }}
.tm-h {{ display:flex; justify-content:space-between; align-items:baseline; font-size:13.5px; color:var(--muted) }}
.tm-h b {{ color:var(--ink); font:600 17px "Barlow Semi Condensed",sans-serif }}
.tm-v {{ font:700 20px "Barlow Semi Condensed",sans-serif; margin-top:2px }}
.tm.q-strong .tm-v, .tm.q-good .tm-v {{ color:var(--long) }} .tm.q-weak .tm-v {{ color:var(--short) }} .tm.q-flip .tm-v {{ color:var(--flip) }}
.qfoot .k-tp {{ color:var(--long); font-weight:600 }} .qfoot .k-sl {{ color:var(--short); font-weight:600 }}
.qlabel {{ font-size:14px; color:var(--muted) }}
.hist {{ margin:8px 0 0; font-size:13px; color:var(--muted) }}
.note {{ margin:4px 0 0; font-size:13px; color:var(--muted) }}

.empty {{ background:var(--surface); border:1px dashed var(--line); border-radius:12px; padding:14px 16px; margin:0; color:var(--muted) }}

.waits {{ list-style:none; padding:0; margin:0; background:var(--surface); border:1px solid var(--line); border-radius:12px }}
.waits li {{ display:grid; grid-template-columns:minmax(150px,1.1fr) minmax(160px,1fr) auto auto; align-items:center; gap:6px 18px; padding:12px 16px; border-top:1px solid var(--line) }}
.waits li:first-child {{ border-top:0 }}
.w-name b {{ display:block; font-size:17px }} .w-name span {{ font-size:13px; color:var(--muted) }}
.waits .strip {{ margin:0; max-height:44px }}
.w-q {{ display:flex; flex-direction:column; font-size:13.5px; color:var(--muted); text-align:right }}

details.sess {{ background:var(--surface); border:1px solid var(--line); border-radius:12px; margin-bottom:14px }}
details.sess > summary {{ list-style:none; cursor:pointer; display:flex; align-items:baseline; flex-wrap:wrap; gap:4px 14px; padding:14px 16px }}
details.sess > summary::-webkit-details-marker {{ display:none }}
details.sess > summary::before {{ content:"▸"; color:var(--muted); transition:transform .15s }}
details.sess[open] > summary::before {{ transform:rotate(90deg) }}
details.sess > summary span {{ color:var(--muted) }}
summary:focus-visible, a:focus-visible {{ outline:2px solid var(--range); outline-offset:2px; border-radius:8px }}
.scroll {{ overflow-x:auto; border-top:1px solid var(--line) }}
table {{ border-collapse:collapse; width:100%; min-width:820px }}
th, td {{ text-align:left; padding:9px 12px; vertical-align:top; font-size:14.5px }}
thead th {{ font-size:12.5px; color:var(--muted); font-weight:600; border-bottom:1px solid var(--line) }}
tbody th {{ font-weight:600; white-space:nowrap }}
tr.grp th {{ font:600 15px "Barlow Semi Condensed",sans-serif; color:var(--muted); padding-top:16px; background:var(--bg) }}
tbody tr + tr td, tbody tr + tr th {{ border-top:1px solid var(--line) }}
td small, th small {{ display:block; color:var(--muted); font-size:12.5px }}
.st {{ display:flex; align-items:center; gap:7px; font-weight:500 }}
.dot {{ width:8px; height:8px; border-radius:50%; background:var(--line); flex:none }}
.dot.live {{ background:var(--long); box-shadow:0 0 0 3px var(--long-soft) }}
.dot.wait {{ background:var(--range) }} .dot.win {{ background:var(--long) }} .dot.loss {{ background:var(--short) }}
tr.st-open td, tr.st-open th {{ background:var(--long-soft) }}
.lv3 {{ display:flex; flex-direction:column; font-size:13.5px }}
.qc b {{ font-family:"Barlow Semi Condensed",sans-serif; font-size:16px }}
.qc.q-strong, .qc.q-good {{ color:var(--long) }} .qc.q-weak {{ color:var(--short) }} .qc.q-flip, .qc.q-none {{ color:var(--muted) }}

.zones {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; margin:0 0 14px }}
.zone {{ background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:12px 14px; display:grid; grid-template-columns:1fr auto; align-items:baseline; gap:0 10px }}
.zone span {{ font:600 18px "Barlow Semi Condensed",sans-serif }}
.zone b {{ font:700 26px "Barlow Semi Condensed",sans-serif; font-variant-numeric:tabular-nums }}
.zone small {{ grid-column:1 / -1; color:var(--muted); font-size:13.5px }}
.zone small em {{ font-style:normal; color:var(--ink); font-weight:600 }}
.zone.open {{ border-color:var(--long); box-shadow:inset 4px 0 0 var(--long) }} .zone.open b {{ color:var(--long) }}
.zone.soon {{ box-shadow:inset 4px 0 0 var(--range) }} .zone.soon b {{ color:var(--range) }}
.zone.done b {{ color:var(--muted) }}
.w-d {{ display:flex; flex-direction:column; font-size:13.5px; color:var(--muted) }}
.w-d b {{ color:var(--ink); font-weight:600 }}
.fc {{ grid-column:1 / -1; margin:2px 0 0; display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; font-size:14px; color:var(--muted) }}
.fc .eta {{ font:700 20px "Barlow Semi Condensed",sans-serif; color:var(--range); white-space:nowrap }}
.fc b {{ color:var(--ink) }} .fc.none {{ font-size:13px }}
.bot-box {{ background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:16px }}
.bot h3 {{ font:600 19px "Barlow Semi Condensed",sans-serif; margin:18px 0 6px }}
.bot .sum {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:12px }}
.bot .sum div {{ display:flex; flex-direction:column }}
.bot .sum span {{ font-size:13px; color:var(--muted) }}
.bot .sum b {{ font:700 32px/1.1 "Barlow Semi Condensed",sans-serif; font-variant-numeric:tabular-nums }}
.bot .sum small {{ font-size:13px; color:var(--muted) }}
.bot .sum.up div:nth-child(2) b {{ color:var(--long) }} .bot .sum.down div:nth-child(2) b {{ color:var(--short) }}
.eq {{ display:block; width:100%; height:auto; background:var(--bg); border-radius:8px }}
.eq-line {{ fill:none; stroke-width:2.5; stroke-linejoin:round }} .eq-line.up {{ stroke:var(--long) }} .eq-line.down {{ stroke:var(--short) }}
.eq-area.up {{ fill:var(--long-soft) }} .eq-area.down {{ fill:var(--short-soft) }}
.eq-base {{ stroke:var(--muted); stroke-dasharray:4 4; stroke-width:1 }}
.eq-lab {{ font:500 11px Barlow,sans-serif; fill:var(--muted) }}
.blog {{ list-style:none; margin:0; padding:0 }}
.blog li {{ display:grid; grid-template-columns:96px 1fr 1.2fr auto; gap:4px 12px; padding:7px 0; border-top:1px solid var(--line); font-size:14.5px; align-items:baseline }}
.blog li span {{ color:var(--muted) }}
.blog em {{ font:700 16px "Barlow Semi Condensed",sans-serif; font-style:normal; font-variant-numeric:tabular-nums; text-align:right }}
.blog em.pos {{ color:var(--long) }} .blog em.neg {{ color:var(--short) }}
.bust {{ color:var(--short); font-weight:600 }}
@media (max-width:640px) {{
  .blog li {{ grid-template-columns:1fr auto; }}
  .blog li span:first-child {{ grid-column:1 / -1; font-size:12.5px }}
  .blog li span:nth-of-type(2) {{ grid-column:1; grid-row:3; font-size:13px }}
  .blog li em {{ grid-row:2 / span 2; grid-column:2 }}
}}
.calls {{ margin-bottom:26px }}
.calllist {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:10px }}
.call {{ display:grid; grid-template-columns:70px minmax(150px,1fr) minmax(240px,1.6fr) minmax(170px,1.1fr) minmax(140px,.9fr);
  gap:8px 16px; align-items:center; background:var(--surface); border:1px solid var(--line); border-radius:12px;
  padding:14px 16px; position:relative; overflow:hidden }}
.call::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:var(--long) }}
.call.short::before {{ background:var(--short) }}
.c-time {{ font:700 22px "Barlow Semi Condensed",sans-serif; line-height:1 }}
.c-time small {{ display:block; font:500 12.5px Barlow,sans-serif; color:var(--muted); margin-top:3px }}
.c-main b {{ display:block; font:700 24px/1.05 "Barlow Semi Condensed",sans-serif }}
.c-main .dir {{ display:inline-block; margin-top:4px; font-size:14px; padding:1px 8px }}
.c-lv {{ display:flex; flex-direction:column; font-size:14px; color:var(--muted) }}
.c-lv b {{ color:var(--ink); font-variant-numeric:tabular-nums }}
.c-q {{ display:flex; align-items:center; gap:8px; font-size:12.5px; color:var(--muted); line-height:1.25 }}
.c-q .qnum {{ color:var(--long) }}
.c-st {{ font:600 16px "Barlow Semi Condensed",sans-serif; text-align:right }}
.call.won .c-st {{ color:var(--long) }} .call.lost .c-st {{ color:var(--short) }} .call.run .c-st {{ color:var(--range) }}
.call.won {{ background:linear-gradient(90deg, var(--surface) 70%, var(--long-soft)) }}
.call.lost {{ background:linear-gradient(90deg, var(--surface) 70%, var(--short-soft)) }}
@media (max-width:760px) {{
  .call {{ grid-template-columns:auto 1fr; gap:8px 14px }}
  .c-lv, .c-q, .c-st {{ grid-column:1 / -1 }}
  .c-lv {{ flex-direction:row; flex-wrap:wrap; gap:2px 14px }}
  .c-st {{ text-align:left; font-size:17px }}
}}
.fine {{ margin-top:36px; padding-top:16px; border-top:1px solid var(--line); color:var(--muted); font-size:14px }}
.fine h2 {{ font-size:20px; color:var(--ink); margin-bottom:6px }}
.fine p {{ max-width:78ch; margin:0 0 10px }}
.fine b {{ color:var(--ink) }}

@media (max-width:640px) {{
  body {{ font-size:16px }}
  main {{ padding:18px 14px 48px }}
  h1 {{ font-size:34px }} h2 {{ font-size:23px }}
  .top {{ margin-bottom:12px }} .count {{ font-size:16px }}
  .sigs {{ grid-template-columns:1fr }}
  .sig h3 {{ font-size:24px }}
  .lv {{ grid-template-columns:repeat(2,1fr); gap:10px }}
  .lv dd {{ font-size:20px }}
  .waits li {{ grid-template-columns:1fr 1fr; gap:8px 12px }}
  .waits .strip {{ grid-column:1 / -1; order:-1 }}
  .w-name {{ grid-column:1 / -1; order:-2 }}
  .w-q {{ text-align:right }}
  .band-s em {{ font-size:10.5px }}
  .tl-ticks span:nth-child(even) {{ display:none }}
  .scroll {{ overflow:visible }}
  table {{ min-width:0 }}
  thead {{ display:none }}
  table, tbody, tr, th, td {{ display:block; width:100% }}
  tbody tr {{ display:grid; grid-template-columns:1fr 1fr; gap:6px 12px; padding:12px 14px }}
  tbody tr + tr td, tbody tr + tr th {{ border-top:0 }}
  tbody tr + tr {{ border-top:1px solid var(--line) }}
  tbody th, tbody td {{ padding:0 }}
  tbody tr > th {{ grid-column:1 / -1; font-size:17px }}
  td[data-l="Status"], td.lvc {{ grid-column:1 / -1 }}
  td.e {{ display:none }}
  tr.st-open {{ background:var(--long-soft) }}
  tr.st-open td, tr.st-open th {{ background:transparent }}
  td[data-l]::before {{ content:attr(data-l); display:block; font-size:11.5px; color:var(--muted) }}
  td[data-l="Status"]::before {{ display:none }}
  tr.grp {{ display:block; padding:0 }} tr.grp th {{ padding:10px 14px 6px }}
  .lv3 {{ flex-direction:row; gap:14px; flex-wrap:wrap }}
  details.sess > summary {{ padding:12px 14px }}
}}
@media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important }} }}
</style></head>
<body><main>
<header class="top">
  <div><h1>Breakout-Board</h1><p>{when}</p></div>
  <p class="count"><b>{len(best)}</b> frische Signale, {n_open} offen insgesamt<br>
  <a class="btlink" href="rueckrechnung.html">Was wäre mit 100 pro Markt herausgekommen?</a></p>
</header>
{zones(rows, now)}
{render_calls(pick_calls(rows), rows, now)}
{timeline(rows, now)}

<section>
<div class="sec"><h2>Weitere frische Signale</h2>
<p>Signal ausgelöst, Kurs noch nah am Einstieg, und dieselbe Regel hat bei diesem Markt in den letzten ~60 Tagen
im Schnitt Gewinn gebracht. Die beste Vorgeschichte steht vorne.</p></div>
{best_html}
</section>

<section>
<div class="sec"><h2>Warten auf Ausbruch</h2>
<p>Spanne steht, noch kein Ausbruch. Der Punkt zeigt, wo der Kurs im Band liegt. Die Zeitschätzung kommt
aus den letzten ~60 Tagen: an Tagen, an denen um diese Uhrzeit auch noch kein Ausbruch da war, wann er dann kam.</p></div>
{wait_html}
</section>

{tables}
{err_html}
{bot_html}

<footer class="fine">
<h2>So liest du das</h2>
<p>Das gelbe Band ist die Eröffnungsspanne, die ersten 15 Minuten einer Session. Schliesst eine 5-Minuten-Kerze
darüber, entsteht ein Long-Signal, darunter ein Short-Signal. Der Stop liegt in der Mitte des Bandes, das Ziel
{TARGET_R:g}× so weit vom Einstieg wie der Stop. R ist das Vielfache des Risikos: +{TARGET_R:g} R heisst Ziel erreicht,
−1 R heisst Stop.</p>
<p>Die <b>TP-Quote</b> sagt, wie oft dasselbe Signal beim gleichen Markt in den letzten ~60 Tagen zuerst den TP
(das Ziel) erreicht hat. Der rote Teil im Balken ist der Anteil, der zuerst in den SL lief, der graue blieb bis
Session-Ende offen. Weil ein Treffer zwei Stops ausgleicht, reichen {be} % (der Strich im Balken).
<b>Stark</b> heisst mindestens 15 Punkte darüber, <b>gut</b> 5 bis 15 darüber, <b>Münzwurf</b> weniger als 5 daneben,
<b>schwach</b> mehr als 5 darunter. Unter {MIN_HISTORY} Fällen gibt es keine Quote. Gebühren und Spread sind nicht
eingerechnet.</p>
<p><b>Keine Anlageberatung.</b> Kostenlose, verzögerte Kurse von Yahoo. Indizes, Gold und Öl sind Futures-Kurse und
weichen vom CFD-Kurs deines Brokers ab, übernimm Stop und Ziel deshalb als Prozent-Abstand. CFDs und Krypto haben oft
einen Hebel, damit kannst du mehr verlieren als hier steht. Die Vergangenheit sagt nichts Sicheres über den nächsten
Trade, und die meisten Daytrader verlieren Geld.</p>
</footer>
<script>
(function () {{
  function d(m) {{ m = Math.max(0, Math.round(m)); if (m < 60) return m + " Min."; var h = Math.floor(m / 60), r = m % 60; return h + " Std." + (r ? " " + r + " Min." : ""); }}
  function tick() {{
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-until]").forEach(function (e) {{
      var m = (+e.getAttribute("data-until") - now) / 60;
      e.textContent = m <= 0 ? "jetzt" : (e.tagName === "EM" ? d(m) : "in " + d(m));
    }});
    document.querySelectorAll("[data-at]").forEach(function (e) {{
      var m = (+e.getAttribute("data-at") - now) / 60;
      e.textContent = m <= 1 ? "jeden Moment" : "in ~" + d(m);
    }});
  }}
  tick(); setInterval(tick, 20000);
}})();
</script>
</main></body></html>
"""


# --------------------------------------------------------------------------
# Rueckrechnung: was waere aus 100 pro Markt geworden?
# --------------------------------------------------------------------------

BT_START = 100.0
BT_LEVERAGES = [100, 10]
# Spread hin und zurueck, in % vom Positionswert (grobe CFD-Werte)
BT_COST = {"Devisen": 0.0001, "Indizes": 0.0002, "Rohstoffe": 0.0003, "Krypto": 0.0010}
BT_SCENARIOS = [
    ("stark", {"stark"}, "Nur Signale mit Einstufung „stark“"),
    ("gut", {"stark", "gut"}, "Signale mit „stark“ oder „gut“"),
]
BT = {}   # symbol -> Liste der Trades, wird in evaluate() gefuellt


def bt_trades(bars, sym, group, today, min_risk):
    """Alle vergangenen Signale mit Einstufung NUR aus den Tagen davor (kein Blick in die Zukunft)."""
    trades = []
    tss = [b[0] for b in bars]
    for s in SESSIONS:
        if sym in LONDON_ONLY and s["key"] != "london":
            continue
        days = sorted({local(b[0], s["tz"]).date() for b in bars})
        past = {1: [], -1: []}
        for d in days:
            if d >= today:
                continue
            r = run_session(bars, d, s, min_risk=min_risk)
            if r.get("status") not in ("stop", "target", "closed"):
                continue
            side, entry = r["side"], r["entry"]
            prior = past[side]
            n = len(prior)
            label = "zu wenig Daten"
            if n >= MIN_HISTORY:
                win = sum(1 for x in prior if x >= TARGET_R) / n
                _, label, _ = quality(dict(n=n, target=round(win * n), stop=0, neither=0, avgR=0))
            exit_ts = r.get("exit_ts", r["t_end"])
            if r["status"] == "stop":
                exit_px = r["stop"]
            elif r["status"] == "target":
                exit_px = r["target"]
            else:
                exit_px = r["price"]
            worst = entry
            i = bisect.bisect_left(tss, r["trig_ts"])
            j = bisect.bisect_left(tss, exit_ts)
            for b in bars[i:j]:
                worst = min(worst, b[3]) if side > 0 else max(worst, b[2])
            mae = side * (entry - worst) / entry          # groesste Bewegung gegen den Trade
            if r["status"] == "stop":                     # am Stop ist Schluss
                mae = min(mae, abs(entry - r["stop"]) / entry)
            trades.append(dict(date=d.isoformat(), session=s["key"], side=side, entry_ts=r["trig_ts"],
                               exit_ts=exit_ts, entry=entry, exit=exit_px, R=r["R"], label=label,
                               ret=side * (exit_px - entry) / entry, mae=max(0.0, mae)))
            prior.append(r["R"])
    trades.sort(key=lambda t: t["entry_ts"])
    return trades


def bt_run(trades, labels, lev, cost):
    bal, busy_until = BT_START, 0
    n = wins = liq = 0
    peak, low = BT_START, BT_START
    for t in trades:
        if t["label"] not in labels or t["entry_ts"] < busy_until:
            continue
        busy_until = t["exit_ts"]
        n += 1
        size = bal * lev
        if t["mae"] >= 1 / lev:           # Kurs lief um die ganze Marge gegen uns: Konto leer
            pnl = -bal
            liq += 1
        else:
            pnl = size * t["ret"] - size * cost
        wins += pnl > 0
        bal = max(0.0, bal + pnl)
        peak, low = max(peak, bal), min(low, bal)
        if bal < 0.01:
            bal = 0.0
            break
    return dict(n=n, wins=wins, liq=liq, end=round(bal, 2), peak=round(peak, 2), low=round(low, 2),
                busted=bal == 0.0)


def bt_results():
    res = {}
    for sym, name, group, dec, note in MARKETS:
        trades = BT.get(sym)
        if trades is None:
            continue
        cost = BT_COST.get(group, 0.0003)
        res[sym] = dict(name=name, group=group, ntr=len(trades),
                        first=trades[0]["date"] if trades else None,
                        last=trades[-1]["date"] if trades else None,
                        runs={(k, lev): bt_run(trades, labels, lev, cost)
                              for k, labels, _ in BT_SCENARIOS for lev in BT_LEVERAGES})
    return res


def chf(x):
    return f"{x:,.0f}".replace(",", "'")


def render_bt(res, now):
    stamp = local(now, "ch").strftime("%d.%m.%Y %H:%M")
    firsts = [v["first"] for v in res.values() if v["first"]]
    lasts = [v["last"] for v in res.values() if v["last"]]
    period = (f"{datetime.fromisoformat(min(firsts)).strftime('%d.%m.')} bis "
              f"{datetime.fromisoformat(max(lasts)).strftime('%d.%m.%Y')}") if firsts else "–"
    blocks = ""
    for key, labels, title in BT_SCENARIOS:
        for lev in BT_LEVERAGES:
            runs = {s: v["runs"][(key, lev)] for s, v in res.items()}
            used = {s: r for s, r in runs.items() if r["n"]}
            invest = BT_START * len(used)
            end = sum(r["end"] for r in used.values())
            busted = sum(1 for r in used.values() if r["busted"])
            ntr = sum(r["n"] for r in used.values())
            liq = sum(r["liq"] for r in used.values())
            delta = end - invest
            ends = sorted(r["end"] for r in used.values())
            med = _q(ends, .5) if ends else 0
            gain = sum(1 for e in ends if e > BT_START)
            cls = "up" if delta > 0 else "down"
            rows = ""
            for s, r in sorted(used.items(), key=lambda kv: kv[1]["end"], reverse=True):
                v = res[s]
                st = "Konto leer" if r["busted"] else ""
                rows += (f"<tr class='{'bust' if r['busted'] else ''}'><th scope='row'>{esc(v['name'])}</th>"
                         f"<td data-l='Trades'>{r['n']}</td><td data-l='Gewinner'>{r['wins']}</td>"
                         f"<td data-l='Liquidiert'>{r['liq']}</td>"
                         f"<td data-l='Endstand' class='{'pos' if r['end'] > BT_START else 'neg'}'><b>{chf(r['end'])}</b></td>"
                         f"<td data-l='Tiefster Stand'>{chf(r['low'])}</td><td data-l=''>{st}</td></tr>")
            skipped = len(res) - len(used)
            blocks += f"""
<section class="bt">
<header><h2>{esc(title)}, Hebel {lev}</h2></header>
<div class="sum {cls}">
  <div><span>Eingesetzt</span><b>{chf(invest)}</b><small>{len(used)} Märkte × {chf(BT_START)}</small></div>
  <div><span>Ende</span><b>{chf(end)}</b><small>{'+' if delta >= 0 else '−'}{chf(abs(delta))} ({delta / invest * 100 if invest else 0:+.0f} %)</small></div>
  <div><span>Trades</span><b>{ntr}</b><small>{liq} davon liquidiert</small></div>
  <div><span>Konten im Plus</span><b>{gain}</b><small>von {len(used)}, {busted} ganz leer</small></div>
  <div><span>Typisches Konto</span><b>{chf(med)}</b><small>die Hälfte endet darunter</small></div>
</div>
{'<p class="muted">' + str(skipped) + ' Märkte hatten in diesem Zeitraum kein solches Signal und sind nicht mitgezählt.</p>' if skipped else ''}
<details><summary>Alle Märkte einzeln</summary>
<div class="scroll"><table><thead><tr><th>Markt</th><th>Trades</th><th>Gewinner</th><th>Liquidiert</th>
<th>Endstand</th><th>Tiefster Stand</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></details>
</section>"""

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rückrechnung</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{ --bg:#EEF1F5; --surface:#FFFFFF; --ink:#15233B; --muted:#5D6B80; --line:#D5DCE6;
  --long:#0E8A6A; --long-soft:#DDF2EA; --short:#C8364B; --short-soft:#F8E1E5; --range:#E4A11B; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#0E1625; --surface:#16223A; --ink:#E4EAF3; --muted:#8D9AB0; --line:#26344D;
  --long:#3DCB9E; --long-soft:#123A32; --short:#F0667A; --short-soft:#3F1C26; --range:#F2B544; }} }}
:root[data-theme="dark"] {{ --bg:#0E1625; --surface:#16223A; --ink:#E4EAF3; --muted:#8D9AB0; --line:#26344D;
  --long:#3DCB9E; --long-soft:#123A32; --short:#F0667A; --short-soft:#3F1C26; --range:#F2B544; }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 Barlow,system-ui,sans-serif }}
main {{ max-width:980px; margin:0 auto; padding:24px 16px 56px }}
h1, h2, .sum b {{ font-family:"Barlow Semi Condensed",Barlow,sans-serif }}
h1 {{ font-size:40px; line-height:1.05; margin:0 0 4px }}
h2 {{ font-size:23px; margin:0 0 10px }}
a {{ color:inherit }}
.muted, .lead {{ color:var(--muted) }} .lead {{ max-width:72ch; margin:6px 0 24px }}
.bt {{ background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px }}
.sum {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px }}
.sum div {{ display:flex; flex-direction:column }}
.sum span {{ font-size:13px; color:var(--muted) }}
.sum b {{ font-size:32px; line-height:1.1; font-variant-numeric:tabular-nums }}
.sum small {{ color:var(--muted); font-size:13px }}
.sum.up div:nth-child(2) b {{ color:var(--long) }} .sum.down div:nth-child(2) b {{ color:var(--short) }}
details {{ margin-top:12px }} summary {{ cursor:pointer; color:var(--muted) }}
.scroll {{ overflow-x:auto }}
table {{ border-collapse:collapse; width:100%; margin-top:8px; font-variant-numeric:tabular-nums }}
th, td {{ text-align:left; padding:7px 10px; border-top:1px solid var(--line); font-size:14.5px }}
thead th {{ font-size:12.5px; color:var(--muted); border-top:0 }}
td.pos {{ color:var(--long) }} td.neg {{ color:var(--short) }}
tr.bust td:last-child {{ color:var(--short); font-weight:600 }}
.how {{ margin-top:28px; color:var(--muted); font-size:14px }} .how p {{ max-width:76ch }} .how b {{ color:var(--ink) }}
@media (max-width:640px) {{
  h1 {{ font-size:32px }}
  .sum {{ grid-template-columns:1fr 1fr }}
  .sum b {{ font-size:28px }}
  thead {{ display:none }}
  table, tbody, tr, th, td {{ display:block }}
  tbody tr {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:2px 10px; padding:10px 0; border-top:1px solid var(--line) }}
  tbody th {{ grid-column:1 / -1; border:0; padding:0; font-size:16px }}
  tbody td {{ border:0; padding:0 }}
  td[data-l]::before {{ content:attr(data-l); display:block; font-size:11.5px; color:var(--muted) }}
}}
</style></head><body><main>
<p><a href="./">← Zurück zum Board</a></p>
<h1>Was wäre herausgekommen?</h1>
<p class="lead">Rückrechnung vom {period}: Jeder Markt bekommt ein eigenes Konto mit {chf(BT_START)}. Gewinne und
Verluste bleiben drin, der nächste Trade läuft mit dem neuen Kontostand. Berechnet am {stamp} Uhr.</p>
{blocks}
<div class="how">
<h2>So wurde gerechnet</h2>
<p><b>Kein Blick in die Zukunft.</b> Ob ein Signal „stark“ oder „gut“ war, entscheidet nur, wie dieselbe Regel an
den Tagen <b>davor</b> abgeschnitten hat. Die ersten Wochen fehlen deshalb, weil es noch zu wenig Vorgeschichte gab.</p>
<p><b>Hebel {BT_LEVERAGES[0]}:</b> Mit {chf(BT_START)} bewegst du {chf(BT_START * BT_LEVERAGES[0])}. Läuft der Kurs während des
Trades um 1 % gegen dich, ist das ganze Konto weg („liquidiert“), auch wenn der Stop weiter entfernt liegt. Viele
Broker schliessen schon bei 50 % Verlust der Marge, dann wäre es noch schlechter. Zum Vergleich steht jeweils auch
Hebel {BT_LEVERAGES[1]} da.</p>
<p><b>Ablauf:</b> Einstieg zum Schlusskurs der Ausbruchskerze, Ausstieg am Stop, am Ziel oder am Session-Ende.
Pro Konto immer nur ein Trade gleichzeitig. Spread hin und zurück: Devisen 0,01 %, Indizes 0,02 %, Rohstoffe
0,03 %, Krypto 0,10 % vom Positionswert. Übernacht-Gebühren entfallen, weil alles am selben Tag geschlossen wird.
Ein Stop kann in echt schlechter ausgeführt werden.</p>
<p><b>Wichtig:</b> Das sind gut 6 Wochen Vergangenheit, kein Versprechen. Für Privatkunden in der Schweiz und in der EU
erlauben regulierte Broker meist höchstens Hebel 30 bei Devisen, 20 bei Indizes und Gold und 2 bei Krypto.</p>
</div>
</main></body></html>
"""


# --------------------------------------------------------------------------
# Bot-Simulator: 300 Start, pro Session die ersten 3 guten Signale, je 1/3 des Vermoegens, Hebel 100
# --------------------------------------------------------------------------

BOT_START = 300.0
BOT_LEV = 100
BOT_SLOTS = 3
BOT_LABELS = {"stark", "gut"}


def _cost(sym):
    g = next((m[2] for m in MARKETS if m[0] == sym), "")
    return BT_COST.get(g, 0.0003)


def _name(sym):
    return next((m[1] for m in MARKETS if m[0] == sym), sym)


def bot_sim():
    sessions = {}
    for sym, trades in BT.items():
        for t in trades:
            if t["label"] in BOT_LABELS:
                sessions.setdefault((t["date"], t["session"]), []).append(dict(t, sym=sym))
    keys = sorted(sessions, key=lambda k: min(t["entry_ts"] for t in sessions[k]))
    eq, curve, log = BOT_START, [], []
    n = wins = liq = 0
    peak, maxdd = BOT_START, 0.0
    if keys:
        curve.append((min(t["entry_ts"] for t in sessions[keys[0]]), eq))
    for k in keys:
        picks = sorted(sessions[k], key=lambda t: t["entry_ts"])[:BOT_SLOTS]
        stake = eq / BOT_SLOTS
        total = 0.0
        for t in picks:
            size = stake * BOT_LEV
            if t["mae"] >= 1 / BOT_LEV:
                pnl, how = -stake, "liquidiert"
                liq += 1
            else:
                pnl = max(-stake, size * t["ret"] - size * _cost(t["sym"]))
                how = "TP" if t["R"] >= TARGET_R else ("SL" if t["R"] <= -1 else "Session-Ende")
            n += 1
            wins += pnl > 0
            total += pnl
            log.append(dict(ts=t["entry_ts"], sym=t["sym"], side=t["side"], label=t["label"],
                            stake=stake, pnl=pnl, how=how))
        eq = max(0.0, eq + total)
        end_ts = max(t["exit_ts"] for t in picks)
        curve.append((end_ts, eq))
        peak = max(peak, eq)
        if peak:
            maxdd = max(maxdd, (peak - eq) / peak)
        if eq < 1:                    # unter 1 Franken kann der Bot nicht mehr sinnvoll handeln
            eq, bust_ts = 0.0, end_ts
            break
    else:
        bust_ts = None
    if not keys:
        bust_ts = None
    return dict(eq=eq, curve=curve, log=log, n=n, wins=wins, liq=liq, maxdd=maxdd, sessions=len(keys),
                bust_ts=bust_ts, first_ts=curve[0][0] if curve else None)


def bot_today(rows, eq):
    """Was der Bot heute haette: pro Session die ersten 3 Signale mit stark/gut."""
    out = []
    for s in SESSIONS:
        cand = []
        for r in rows:
            if r["session"] != s["key"] or r.get("status") not in ("open", "target", "stop", "closed"):
                continue
            _, label, _ = quality(r["hist"][r["side"]])
            if label in BOT_LABELS:
                cand.append((r, label))
        cand.sort(key=lambda x: x[0]["trig_ts"])
        for r, label in cand[:BOT_SLOTS]:
            stake = eq / BOT_SLOTS
            if r["status"] == "target":
                px = r["target"]
            elif r["status"] == "stop":
                px = r["stop"]
            else:
                px = r["price"]
            ret = r["side"] * (px - r["entry"]) / r["entry"]
            pnl = max(-stake, stake * BOT_LEV * (ret - _cost(r["symbol"])))
            out.append(dict(r=r, label=label, stake=stake, pnl=pnl, ret=ret))
    return out


def equity_svg(curve, w=640, h=170):
    if len(curve) < 2:
        return ""
    vals = [max(v, 0.01) for _, v in curve]
    import math
    lo, hi = min(vals + [BOT_START]), max(vals + [BOT_START])
    logscale = hi / lo > 20
    f = (lambda v: math.log10(v)) if logscale else (lambda v: v)
    a, b = f(lo), f(hi)
    if b == a:
        b = a + 1
    t0, t1 = curve[0][0], curve[-1][0]

    def X(t):
        return 6 + (t - t0) / max(1, t1 - t0) * (w - 12)

    def Y(v):
        return 10 + (1 - (f(max(v, 0.01)) - a) / (b - a)) * (h - 30)

    pts = " ".join(f"{X(t):.1f},{Y(v):.1f}" for t, v in curve)
    up = curve[-1][1] >= BOT_START
    base = Y(BOT_START)
    area = f"{X(t0):.1f},{h - 20} " + pts + f" {X(t1):.1f},{h - 20}"
    d0 = local(t0, "ch").strftime("%d.%m.")
    d1 = local(t1, "ch").strftime("%d.%m.")
    note = "<text x='8' y='14' class='eq-lab'>logarithmische Skala</text>" if logscale else ""
    return (f"<svg class='eq' viewBox='0 0 {w} {h}' role='img' aria-label='Kontoverlauf des Bots'>"
            f"<polygon points='{area}' class='eq-area {'up' if up else 'down'}'/>"
            f"<line x1='6' x2='{w - 6}' y1='{base:.1f}' y2='{base:.1f}' class='eq-base'/>"
            f"<text x='{w - 8}' y='{base - 5:.1f}' class='eq-lab' text-anchor='end'>Start {chf(BOT_START)}</text>"
            f"<polyline points='{pts}' class='eq-line {'up' if up else 'down'}'/>"
            f"<text x='6' y='{h - 4}' class='eq-lab'>{d0}</text>"
            f"<text x='{w - 6}' y='{h - 4}' class='eq-lab' text-anchor='end'>{d1}</text>"
            f"{note}"
            f"</svg>")


def render_bot(sim, today):
    eq = sim["eq"]
    delta = eq - BOT_START
    cls = "up" if delta >= 0 else "down"
    rate = f"{sim['wins'] / sim['n'] * 100:.0f} %" if sim["n"] else "–"
    last = ""
    for x in reversed(sim["log"][-10:]):
        last += (f"<li><span>{local(x['ts'], 'ch').strftime('%d.%m. %H:%M')}</span>"
                 f"<b>{esc(_name(x['sym']))}</b><span>{side_word(x['side'])}, {x['how']}</span>"
                 f"<em class='{'pos' if x['pnl'] > 0 else 'neg'}'>{'+' if x['pnl'] > 0 else '−'}{chf(abs(x['pnl']))}</em></li>")
    td = ""
    for x in today:
        r = x["r"]
        st = STATUS.get(r["status"], r["status"])
        td += (f"<li><span>{ch_time(r['trig_ts'])}</span><b>{esc(r['name'])}</b>"
               f"<span>{side_word(r['side'])}, {esc(st)}, {x['label']}</span>"
               f"<em class='{'pos' if x['pnl'] > 0 else 'neg'}'>{'+' if x['pnl'] > 0 else '−'}{chf(abs(x['pnl']))}</em></li>")
    if sim.get("bust_ts"):
        td = ""
    today_html = (f"<ul class='blog'>{td}</ul>" if td else
                  "<p class='empty'>Heute hat der Bot noch nichts gekauft. Er wartet auf die ersten Signale mit "
                  "„stark“ oder „gut“.</p>")
    busted = ""
    if sim.get("bust_ts"):
        busted = (f"<p class='bust'>Am {local(sim['bust_ts'], 'ch').strftime('%d.%m.%Y')} war das Konto leer "
                  f"(unter 1). Ab da hätte der Bot nicht mehr handeln können.</p>")
    return f"""
<section class="bot" id="bot">
<div class="sec"><h2>Bot-Simulator</h2>
<p>Ein gedachter Bot startet mit {chf(BOT_START)}. In jeder Session kauft er die ersten {BOT_SLOTS} Signale mit
Einstufung „stark“ oder „gut“, jedes mit einem Drittel seines ganzen Vermögens und Hebel {BOT_LEV}. Gewinne und
Verluste fliessen sofort ins nächste Drittel.</p></div>
<div class="bot-box">
<div class="sum {cls}">
  <div><span>Start</span><b>{chf(BOT_START)}</b></div>
  <div><span>Jetzt</span><b>{chf(eq)}</b><small>{'+' if delta >= 0 else '−'}{chf(abs(delta))} ({delta / BOT_START * 100:+.0f} %)</small></div>
  <div><span>Trades</span><b>{sim['n']}</b><small>{rate} im Plus, {sim['liq']} liquidiert</small></div>
  <div><span>Grösster Rückgang</span><b>{sim['maxdd'] * 100:.0f} %</b><small>vom Höchststand</small></div>
</div>
{equity_svg(sim['curve'])}
{busted}
<h3>Heute</h3>
{today_html}
<h3>Letzte Trades</h3>
<ul class="blog">{last or '<li>Noch keine.</li>'}</ul>
<p class="hist">Rückrechnung über {sim['sessions']} Sessions ohne Blick in die Zukunft: Die Einstufung eines Signals
kennt nur die Tage davor. Mit Hebel {BOT_LEV} ist ein Drittel weg, sobald der Kurs 1 % gegen den Trade läuft. Mehr als
den Einsatz verliert ein Trade nicht. London und New York überschneiden sich am Nachmittag; vereinfacht rechnet der
Bot New York mit dem Stand nach London. Spread ist eingerechnet, Ausführung zum Schlusskurs der Signal-Kerze.</p>
</div>
</section>"""


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
    res = bt_results()
    with open(os.path.join(a.out, "rueckrechnung.html"), "w", encoding="utf-8") as f:
        f.write(render_bt(res, now))
    with open(os.path.join(a.out, "rueckrechnung.json"), "w", encoding="utf-8") as f:
        json.dump({s: dict(v, runs={f"{k}-{lev}": r for (k, lev), r in v["runs"].items()})
                   for s, v in res.items()}, f, ensure_ascii=False, indent=1)
    ok = len(MARKETS) - len(errors)
    print(f"{ok} von {len(MARKETS)} Maerkten geladen, {len(best)} frische Signale, "
          f"{len(waiting)} warten. -> {os.path.join(a.out, 'index.html')}")
    for e in errors:
        print("  Fehler:", e, file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
