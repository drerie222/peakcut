# Gaming-Highlights im Livestream

Bei Gaming-Aufnahmen übersprang PeakCut bisher die fortlaufende Transkription. Der geplante KI-Check erhielt dadurch keine Wörter und der anschließende Bildpfad verlangte einen Lautstärke- oder Chat-Sprung. Die Anzeige konnte deshalb „No verified gameplay highlight“ melden, obwohl Gemini die Szene nie gesehen hatte.

PeakCut sichert jetzt einen ersten ruhigen Ausschnitt, sobald etwa 75 Sekunden durchgehendes Material verfügbar sind. Weitere Ausschnitte folgen auf der eingestellten Kadenz. Lautstärke- und Chat-Sprünge sichern weiterhin vorrangig ein Fenster mit Vor- und Nachlauf. Überlappende Zeitfenster sowie identische Bild- und Sprachdaten kosten keine weitere Auswertung. Bis zu fünf zeitlich verteilte, auf 480 Pixel Breite verkleinerte Bilder zeigen auch eine im Stream sichtbare Facecam. PeakCut ergänzt kurze lokale Transkripte und Zehn-Sekunden-Zusammenfassungen der Lautstärke und verfügbaren Chat-Zählungen. Chat-Inhalte und Nutzernamen werden dabei nicht übertragen.

Das Modell sucht nach einem belegten Einstieg und einer Pointe oder Wendung. Die Zeitmarken müssen zum zurückgehaltenen Material und zur eingestellten Clipdauer passen. Gesichtsausdrücke und ruhige Dialoge können ebenso ein Highlight stützen wie eine Spielaktion. Unbekannte Sprecher bleiben unzugeordnet. Die gemischte Transkription dient nur der Beurteilung und liefert keine Zitate für Hooks oder Untertitel. Untertitel sind weiterhin auf eine ausdrücklich konfigurierte, verifizierte Mikrofonspur beschränkt. Neue automatische Clips werden erst nach der vorhandenen Bild-, Tonspur- und Dauerprüfung als fertig gemeldet. Beanstandete Renderings bleiben zur Prüfung im Editor und erreichen weder Discord noch Buffer automatisch. Der Gaming-Standard bleibt **Full Gameplay / TikTok**.

## Einstellungen

| Variable | Standard | Wirkung |
| --- | ---: | --- |
| `PEAKCUT_LIVE_HIGHLIGHT_INTERVAL` | 300 s | Mindestabstand zwischen zwei planmäßigen Gaming-Checks. Ein längerer Wert im Creator-Setup gilt weiter. Sprünge werden unabhängig davon geprüft. |
| `PEAKCUT_LIVE_HIGHLIGHT_REQUESTS` | 16 | Tatsächliche HTTP-Versuche je gleitender Streamstunde. Fallbacks, Wiederholungen und unterstützende Facecam-Abfragen zählen mit. `0` stoppt Live-KI-Anfragen. Bei einem wiederaufgenommenen Watchlist-Broadcast gilt die bisherige Stundenzählung weiter. |
| `PEAKCUT_LIVE_HIGHLIGHT_SPEECH` | 60 s | Maximale Länge des Sprachkontexts je Kandidat. `0` lässt nur Bilder und Signale auswerten. Der Höchstwert beträgt 75 s. |
| `PEAKCUT_AI_RPM`, `PEAKCUT_AI_TPM`, `PEAKCUT_AI_RPD` | Bestehende Werte | Gemeinsame API-Grenzen pro konfiguriertem Modell. `PEAKCUT_AI_RPD` zählt über alle Anfragen am selben Modell und Schlüssel bis zum Tageswechsel in der Pacific-Zeitzone. Trage dort das tatsächliche Tageskontingent deines Projekts ein. |
| `PEAKCUT_LIVE_CANDIDATES`, `PEAKCUT_LIVE_CANDIDATE_HOURS` | 16 / 30 h | Globaler Rückstau und Aufbewahrungsfrist für temporäre Kandidaten. Je Aufnahme warten höchstens drei planmäßige Fenster auf Prüfung oder Rendering. Kandidaten verwenden Hardlinks zu den Aufnahmesegmenten. |

Die Werte gehören in die bestehende `.env`. Es werden nur die bereits konfigurierten Anbieter und Modelle verwendet. Gemini-429-Antworten werden über den gemeinsamen Limiter behandelt. Er berücksichtigt `Retry-After`, Backoff und das Tageskontingent. Nach drei tatsächlichen Fehlschlägen bleibt der Kandidat bis zum Ablauf seiner Aufbewahrung zur manuellen Wiederholung verfügbar. Das Dashboard zeigt API-Versuche, abgeschlossene Reviews, Gründe, nächste Versuchszeit und Zeiten für Frames, Sprache, KI, Rendering und Export. Ein fehlendes Highlight, eine ausgebliebene Anfrage und ein API-Fehler haben getrennte Zustände.

## Offline-Prüfung

```bash
python tests/test_entertainment.py
python tests/run.py
CHROMIUM_PATH=/usr/bin/chromium node tests/test_highlight_dashboard.cjs
```

Der Lasttest nutzt das bereits vorhandene Parakeet-INT8-Modell und dessen WAV-Testdatei. Zwei Linux-CPUs aus der lokal verfügbaren Affinitätsmaske einsetzen.

```bash
(
  ulimit -v 2883584
  PEAKCUT_MODELS="$PWD/data/models" PEAKCUT_BENCH_LIVE_HIGHLIGHT=1 PEAKCUT_BENCH_1080P60=1 \
    PEAKCUT_ASR_COPIES=1 PEAKCUT_ASR_THREADS=1 PEAKCUT_FFMPEG_THREADS=1 \
    PEAKCUT_MIN_FREE_RAM_GB=0.55 PEAKCUT_OUT_FPS=24 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
    taskset -c 0,1 python tests/benchmark_small.py
)
```

Der Test erzeugt synthetische 1080p60-Segmente und simuliert die Gemini-Antwort. Er führt Framesampling, lokale Transkription, den API-Limiter, den Schnitt und die Qualitätsprüfung tatsächlich aus. Die 66 Sekunden Quelle ergaben einen 59 Sekunden langen 1080 × 1920-Clip mit vollständig sichtbarem 1080 × 608-Gameplay. FFmpeg brauchte 3,80 s für die Bildauswahl und 31,17 s für das Rendering. Die lokale Transkription brauchte 11,32 s. Der Gesamtlauf mit der Erzeugung des Testmaterials dauerte 96 s. PeakCut erreichte 1,24 GiB Prozess-RSS auf zwei zugewiesenen CPUs mit 2,75 GiB Adressraum pro Prozess. Die temporären Segmente belegten rund 258 MiB, die übertragene JPEG-Datei rund 48 KiB vor Base64-Kodierung. Es gab einen simulierten API-Versuch.

Die geplanten Fenster decken Teile der Aufnahme ab. Sehr kurze Ereignisse zwischen Bildern können unentdeckt bleiben. Die Qualität der Gemini-Entscheidungen, echte Twitch-Aufnahmen und die Gesamtlast auf dem Hetzner-Server brauchen einen separaten Live-Test. Während der Entwicklung gab es keine Veröffentlichung und kein Deployment.
