# Gaming-Clips vor dem Buffer-Entwurf prüfen

## Vorher und nachher

![Vier Standbilder aus demselben synthetischen Testvideo. Vorher startet der Clip schwarz, zeigt eine kleine Facecam und einen Spielsatz. Nachher sind der erste Frame sichtbar, die Facecam größer, das HUD vollständig und die Untertitel stammen aus der getrennten Mikrofonspur.](examples/clip-quality-before-after.jpg)

Die Beispiele vergleichen den Renderer aus `main` (Commit `705b184`) mit der neuen Ausgabe. Beide verarbeiten dasselbe synthetische Material mit 1,25 Sekunden Schwarz am Anfang, einer Kameraattrappe links und einem HUD rechts. Die Beispiele verwenden simulierte ASR-Wörter für die beiden Tonspuren. Das ursprüngliche IShowSpeed-Material stand für diese Prüfung nicht zur Verfügung.

| Fall | Vorher | Nachher |
| --- | --- | --- |
| Schwarzer Vorlauf | Der Render startet im schwarzen Abschnitt. | PeakCut sucht im gewählten Schnitt nach dem ersten stabil sichtbaren Bild, verschiebt den Start bei Bedarf und prüft auch die gerenderte MP4. Wenn dadurch der erkannte Höhepunkt entfällt oder der verbleibende Schnitt zu kurz wird, bleibt der Entwurf zurückgehalten. |
| Facecam und HUD | Die Kamera wird klein in der Ecke gezeigt. Der mittige Gameplay-Crop schneidet Bildränder ab. | Die erkannte Kamerabox bekommt etwas Platz an den Rändern. Die Kamera steht größer in einem eigenen Bereich und das Gameplay bleibt vollständig sichtbar. Hook und Untertitel haben getrennte Positionen. |
| Spielsatz im gemischten Ton | `Getaway driver now` landet als Untertitel im Clip, obwohl der Satz aus dem Spiel stammt. | Ohne getrennte Sprachquelle blendet PeakCut keine Sprecher-Untertitel ein. Mit der bestätigten Mikrofonspur erscheint hier nur `Streamer reaction`. Ein als Zitat formulierter unsicherer Hook entfällt. Ein kurzer beschreibender Hook kann den sichtbaren Moment einleiten. |
| Unklarer Höhepunkt | Spieltext oder Lautstärke kann wie ein gutes Transkript-Highlight aussehen. | Für gemischten Ton dienen Lautstärke und Chat als Marker für eine Stichprobe aus dem Bild. Nur ein hinreichend bewerteter Moment mit sichtbarer Entwicklung oder bestätigter Streamersprache kann die Highlight-Prüfung bestehen. |

## Getrennte Sprache einrichten

1. Prüfe lokal, ob die Quelle zusätzlich zum Programmton eine dauerhaft getrennte Spur mit ausschließlich dem Streamermikrofon enthält. Bei öffentlichen Streams mit nur einer gemischten Spur bleibt die Einstellung aus. Ein Spurname oder die Anwesenheit einer Facecam reicht als Bestätigung nicht aus.
2. Wähle im Creator-Setup unter **Isolated streamer microphone** die bestätigte Spur ab **Audio track 2**. Die Einstellung gilt für neue Aufnahmen und lokale Datei-Uploads dieses Creators. Der Programmaudiomix bleibt der hörbare Ton des Clips. Die zweite Spur liefert nur die Wörter für Untertitel und, bei lokalen Uploads, die gesprochene Grundlage für Highlights.
3. Prüfe einen Clip im Editor. Fehlende Spuren, Clips über mehrere Aufnahmeverbindungen und alte Live-Dateien ohne belegte Mikrofonspur bekommen keine Sprecher-Untertitel. Auch Plattformuntertitel werden für Gaming nicht als Nachweis für die Identität verwendet. Spielton und Kamera allein liefern keine zuverlässige Sprechertrennung. Stimmen, die bereits in einer einzigen Spur gemischt sind, werden nicht getrennt.

Das Verfahren nutzt das bestehende lokale Sprachmodell. Die Bildprüfung dekodiert nur kurze Stichproben und verwendet den bestehenden Ressourcenwächter. Falls eine konfigurierte Bild-KI keinen eindeutigen Höhepunkt erkennt oder gar keine Bild-KI verfügbar ist, bleiben reine Audio-Kandidaten vom Buffer-Entwurf ausgeschlossen. Ruhige Spielmomente können dabei übersehen werden.

## Qualitätsprüfung und vorhandene Clips

- PeakCut kontrolliert Lesbarkeit, Hochformat, Laufzeit und den Start der gerenderten Datei. Bei Gaming prüft es zusätzlich die sichtbare Bewegung im Quellabschnitt und den passenden Nachweis für den vorgeschlagenen Höhepunkt. Das Dashboard erklärt, warum ein Buffer-Entwurf zurückgehalten wurde.
- Der Editor kann ältere Gaming-Clips aus ihren vorhandenen Rohdaten erneut rendern. Vorher werden ältere gerenderte Gaming-Dateien ohne die neue Prüfung für neue Buffer-Entwürfe gesperrt. Bei einem erneuten Rendern können schwache oder unbestätigte Highlights weiterhin zurückgehalten werden. Bereits angelegte Buffer-Entwürfe und Aufnahmen bleiben erhalten.
- Der Export endet beim Buffer-Entwurf. Die Prüfung erzeugt keine öffentliche Veröffentlichung. Für den Test lief keine Installation auf Hetzner.

## Test auf kleinen Ressourcen

Der Offline-Benchmark `tests/benchmark_small.py` dekodierte mit INT8-Parakeet die getrennte Spur einer 66-Sekunden-Quelle und renderte einen **59,3-Sekunden-Clip in 1080 × 1920**. PeakCut erkannte 206 Wörter, benötigte **67,2 Sekunden** und erreichte **1,30 GiB Prozess-RSS**. Der Prozess war auf zwei logische CPUs und 2,75 GiB Adressraum begrenzt. Die Qualitätsprüfung hielt den reinen Lautstärkefund erwartungsgemäß zurück. Aggregierter Container-RAM und Laufzeit auf echten Hetzner Shared vCPUs wurden dabei nicht gemessen.
