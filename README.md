# Breakout-Dashboard

Eine Webseite, die sich während der Handelszeit alle 5 Minuten selbst aktualisiert und zeigt,
welche Märkte aus ihrer Eröffnungsspanne ausbrechen: Bitcoin und andere Krypto, Nasdaq, S&P 500,
Dow, DAX, SMI, Gold (XAU), Silber, Öl, Erdgas, Kupfer und 11 Devisenpaare.

**Keine Anlageberatung.** Die Seite rechnet nach festen Regeln. Gekauft und verkauft wird nichts.

## Die Regel

| | London-Session | New-York-Session |
| --- | --- | --- |
| Start (Schweizer Zeit) | 09:00 | 15:30 |
| Eröffnungsspanne | 09:00–09:15 | 15:30–15:45 |
| Letzter Einstieg | 12:30 | 21:30 |
| Session-Ende | 17:30 | 22:00 |

Ab Ende Oktober bzw. Ende März verschiebt sich New York eine bzw. zwei Wochen lang um eine Stunde,
weil die USA die Uhr zu einem anderen Datum umstellen. Das rechnet das Skript selbst.

- **Signal:** die erste 5-Minuten-Kerze, die **über** der Spanne schliesst (Long) oder **darunter** (Short).
- **Stop:** Mitte der Spanne. **Ziel:** 2 × das Risiko (2 R).
- **Historie:** dieselbe Regel, beim gleichen Markt und in der gleichen Session, an den letzten ~60 Tagen
  (so weit reichen die kostenlosen 5-Minuten-Daten).
- **Beste frische Signale:** aktiv, höchstens 0,75 R vom Einstieg weggelaufen, und die Historie hat mindestens
  8 Fälle mit positivem Durchschnitt. Sortiert nach diesem Durchschnitt.

## Einrichten (einmalig, ca. 5 Minuten)

1. Auf github.com oben rechts **+ → New repository**. Name z. B. `trading-dashboard`, **Public**, dann
   **Create repository**.
2. Auf der leeren Seite **uploading an existing file** anklicken. Den entpackten Ordner öffnen und
   **alles** hineinziehen, auch den Ordner `.github`. Dann **Commit changes**.
3. **Settings → Pages**. Bei **Source** „**GitHub Actions**“ wählen.
4. **Actions** → links **Breakout-Dashboard** → **Run workflow**. Nach ca. 1 Minute ist die Seite unter
   `https://DEIN-NAME.github.io/trading-dashboard/` erreichbar.

Danach läuft es von selbst, alle 5 Minuten von 08:00 bis 23:55 Schweizer Zeit (im Winter 07:00–22:55).

## Gut zu wissen

- GitHub startet geplante Läufe oft 5–15 Minuten zu spät, bei viel Betrieb auch mal gar nicht. Für Sekunden ist
  das nichts, für eine Übersicht reicht es.
- Hat das Repository 60 Tage lang keine Änderung, schaltet GitHub den Zeitplan ab. Dann unter **Actions** wieder
  einschalten.
- **Kurse weichen von deinem Broker ab.** Indizes, Gold und Öl sind Futures-Kurse (Gold liegt z. B. ein paar
  Dollar über XAU/USD-Spot). Übernimm Stop und Ziel als **Prozent-Abstand** vom Einstieg.
- CFDs und Krypto haben oft einen Hebel. Damit kannst du mehr verlieren, als die Seite zeigt.

## Auf dem eigenen PC testen

```
python orb.py
```

schreibt `site/index.html`, die du im Browser öffnest. Mit `--now "2026-10-22 16:10"` (Schweizer Zeit) lässt
sich ein Zeitpunkt festlegen.

Märkte, Zeiten und Ziel stehen oben in `orb.py` (`MARKETS`, `SESSIONS`, `TARGET_R`).
