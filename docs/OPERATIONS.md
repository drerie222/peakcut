# PeakCut dauerhaft auf Hetzner betreiben

Dieses Dokument ergänzt das vorhandene [Hetzner-Profil](../HETZNER.md) und die [Qualitätsprüfung aus PR #6](CLIP_QUALITY.md).

## Betrieb und Diagnose

| Beobachtung | Verhalten und Prüfstelle |
| --- | --- |
| Gemini meldet HTTP 429 | **AI usage** trennt 429, erfolgreiche HTTP-Antworten, Wartezeit und geschätzte Eingabetokens. Google liefert je nach Fehler Projekt-/Modellkontingente für Anfragen und Tokens, HTTP `Retry-After` oder `RetryInfo`. Nur ein konkreter Tages-/Nullkontingent-Hinweis sperrt das betroffene Modell bis mindestens zum nächsten Tageswechsel in Pazifikzeit. Fehlende Details führen zu begrenztem Backoff. |
| Einzelne Modelle antworten, andere liefern 429 | PeakCut verwendet ausschließlich die vorhandene, ausdrücklich eingestellte Modellliste. Text- und Bildanfragen teilen einen begrenzten FIFO-Zugang mit einem aktiven Request. Gemini hat standardmäßig fünf Anfragen je Modell und rollender Minute. Für das Konto passende `PEAKCUT_AI_RPM`, `PEAKCUT_AI_TPM` und `PEAKCUT_AI_RPD` kann der Betreiber in der bestehenden `.env` eintragen. Die Tokenzahl für die Zulassung ist eine lokale Schätzung. Andere Anwendungen im selben Google-Projekt teilen das Provider-Kontingent. [Gemini-Limits](https://ai.google.dev/gemini-api/docs/rate-limits) |
| Wenige Clips trotz laufender Aufnahme | Die Watchlist zeigt die Aufnahmedauer und getrennte Zeiten für Transkription, Bildprüfung, Rendering und Export. Eine Audio- oder Chat-Spitze legt einen Zeitabschnitt zur Bildprüfung ab. Die Liste zeigt Wartezeit, Entscheidungen und Fehler. Ein 429 lässt den Ausschnitt bis zu 30 Stunden für eine erneute Prüfung verfügbar. Die 16 temporären Kandidaten benutzen Hardlinks zu bereits aufgenommenen Segmenten. Die Watchlist zeigt ausgelassene Kandidaten bei Kapazitätsgrenzen oder fehlenden Segmenten. **Retry** wiederholt nur den fehlgeschlagenen Schritt, solange sein Material vorhanden ist. |
| Twitch bleibt beim Laden | Mit öffentlichem HTTPS und zulässiger Größe startet die Vorschau erst durch einen Klick. PeakCut setzt den tatsächlichen Host als `parent`. Unter 400 px Breite steht **Open on Twitch** bereit. Der Link bleibt bei Alters-, Login- und Browserbeschränkungen oder nach dem Start der eingebetteten Vorschau verfügbar. Der serverseitige Recorder arbeitet unabhängig von der Vorschau und benötigt für öffentliche Streams keine Twitch-OAuth-Verknüpfung. [Twitch-Embed-Vorgaben](https://dev.twitch.tv/docs/embed/video-and-clips/) |
| Aufnahmequalität und Datenmenge | yt-dlp wählt bis 1080p60, abhängig vom Rendition-Angebot. Nach fehlgeschlagenen Verbindungen sinkt die Obergrenze bis 720p beziehungsweise 480p. FFmpeg kopiert den gewählten Video- und Audiostream in die Aufnahme; nur die Tonanalyse verwendet separat dekodiertes PCM. Das Dashboard zeigt je Verbindung aus einem geschlossenen Segment gemessene Auflösung, FPS, Containerbitrate und Audiocodec mit Abtastrate. 8 Mbit/s entsprechen etwa 3,6 GB pro Stunde. Der bestehende 12-GB-Freiraum und die Profileinstellungen begrenzen neue Arbeit. `PEAKCUT_LIVE_HEIGHT` kann bei Bandbreitenproblemen in der bestehenden `.env` abgesenkt werden. |
| Buffer oder TikTok-Kanal ist nicht erreichbar | **Check Buffer connection** prüft den verbundenen, entsperrten TikTok-Kanal lesend. Automatische und manuelle Entwürfe verwenden dieselbe Qualitätsprüfung und Reservierung zur Duplikatvermeidung. Vorübergehend nicht erreichbare Lesezugriffe sind wiederholbar. Eine unbestätigte Schreibanfrage erfordert Abgleich in Buffer, damit kein zweiter Entwurf entsteht. |
| Neustart, Festplatte und Logs | Die Watchlist läuft im Serverprozess ohne Browser. Ein Neustart lädt gespeicherte Job- und Kandidateneinträge. Der Recorder versucht bei unterbrochener Verbindung einen kontrollierten Neustart. Compose rotiert die Logs je Dienst bei 10 MB mit drei Dateien. Aufnahme-Medien-URLs werden aus FFmpegs Fehlerausgabe ferngehalten. Die bestehende Rohdatenfrist wird weiter ausgewertet. Eine getrennte Frist betrifft ausschließlich temporäre Kandidaten, fertige Clips bleiben erhalten. |

Die tatsächliche Gemini-Ressource, das Projektkontingent und der Engpass des bisherigen Produktionslaufs wurden nicht eingesehen. Der HTTPS-Zugriff auf die Produktionsadresse scheiterte in der Testumgebung bereits am ausgehenden Tunnel. Eine niedrige Clipzahl kann sowohl von fehlender visueller Bestätigung als auch von Provider-Limits kommen. Die neuen Statusanzeigen unterscheiden diese Fälle. Bildprüfungen verwenden Stichproben und können leise oder sehr kurze Ereignisse übersehen. Die Duplikaterkennung berücksichtigt sich überschneidende Zeitfenster derselben Aufnahme. Optisch ähnliche Aktionen aus getrennten Streams sind dadurch nicht zuverlässig erkennbar.

## Sprecherzuordnung im Mischton

Der reproduzierbare Versuch `python tests/eval_mixed_speech.py` erzeugt zwei Stimmen mit FFmpeg und mischt beide in die Mitte der Stereospur. Die Differenz L−R war exakt null. Das lokale INT8-Parakeet transkribierte in einem Satz sowohl die Reaktion als auch Spielwörter. Ein Vertauschen der Sprecherrollen ergab denselben Audiomix. Eine Spracherkennung oder eine Sprechergruppierung ohne vertrauenswürdige Referenz belegt darum keine Streamerrolle. Eine unabhängig bestätigte Mikrofonspur bleibt der verifizierte Weg. Gemischte Quellen erhalten keine Sprecher-Untertitel. Der Editor zeigt den Grund. Für eine künftige zusätzliche Methode sind ein zulässig bereitgestelltes Referenzsignal und eine Fehlzuordnungs-Messung auf realem, überlappendem Gameplay nötig. Der synthetische Versuch misst keine Genauigkeit auf Twitch-, YouTube- oder Kick-Originalmaterial.

## Aktualisieren nach Freigabe

Die Befehle werden erst auf dem Server ausgeführt, nachdem ein getesteter Commit freigegeben wurde. Behalte den aktiven Compose-Projektnamen und alle verwendeten Override-Dateien bei. Die folgenden Beispiele zeigen das vorhandene HTTPS-Hetzner-Profil. Bei einer eigenen Proxy-Konfiguration verwende weiterhin deren Compose-Dateien. Führe die Befehle im bestehenden PeakCut-Verzeichnis aus.

```bash
docker compose ls
git status --short
df -h
read -r -p 'Aktiver Compose-Projektname: ' project
pc() { docker compose -p "$project" -f docker-compose.yml -f deploy/docker-compose.hetzner.yml --profile own-https "$@"; }
pc config --quiet
pc ps
```

Plane ein kurzes Aufnahmefenster ohne aktive Aufnahme. Wenn `git status` bestehende lokale Codeänderungen zeigt, sichere sie und kläre ihren Zweck vor dem Checkout. Das gilt auch für den bisherigen manuellen 1080p-Patch. Der neue Standard ist 1080p60, eine vorhandene Einstellungsdatei bleibt maßgeblich für übrige Werte. Berechne genügend Platz für den Medien-Dump auf einem gesicherten Ziel.

```bash
umask 077
backup="$HOME/peakcut-backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup"
git rev-parse HEAD > "$backup/commit"
git diff --binary HEAD > "$backup/local-changes.patch"
cp -p .env docker-compose.yml deploy/docker-compose.hetzner.yml "$backup/"
old_image=$(pc images -q app)
rollback_image="peakcut-rollback:$(date -u +%Y%m%dT%H%M%SZ)"
docker image tag "$old_image" "$rollback_image"
printf '%s\n' "$rollback_image" > "$backup/image"
pc stop app
pc exec -T db pg_dump -U peakcut -Fc peakcut > "$backup/peakcut-postgres.dump"
app_id=$(pc ps -aq app)
docker run --rm --volumes-from "$app_id:ro" -v "$backup:/backup" --entrypoint tar "$rollback_image" \
  -C /data -czf /backup/peakcut-media.tar.gz .
test -s "$backup/peakcut-postgres.dump"
gzip -t "$backup/peakcut-media.tar.gz"
```

Sichere weitere lokale Compose-Overrides und zugehörige Secret-Dateien getrennt mit restriktiven Dateirechten. Wenn ein Sicherungsschritt scheitert, starte den vorhandenen App-Container wieder mit `pc start app` und kläre die Ursache vor der Aktualisierung.

```bash
git fetch origin main
read -r -p 'Freigegebener Commit: ' release
git merge-base --is-ancestor "$release" origin/main
git switch --detach "$release"
pc config --quiet
pc build app
pc up -d --no-build --wait
pc ps
docker stats --no-stream
```

Prüfe auf deiner Domain `/healthz`, die Anmeldung und die Medien eines bekannten älteren Clips. Verwende **Check Buffer connection** vor einem Entwurfsversuch und prüfe vorher `PEAKCUT_DRAFT_ONLY=1` mit dem bestehenden Profil. Der neue Code benötigt keine Änderung an PostgreSQL-Tabellen und keine neuen Zugangsdaten.

### Rollback

Verwende bei Problemen denselben Compose-Projektnamen. Stoppe den App-Dienst und wechsle zum gesicherten Commit. Wenn zusätzliche lokale Compose-Overrides genutzt wurden, wähle deren gesicherten Stand.

```bash
pc stop app
git switch --detach "$(cat "$backup/commit")"
printf 'services:\n  app:\n    image: %s\n    pull_policy: never\n' "$(cat "$backup/image")" > "$backup/rollback.yml"
pc -f "$backup/rollback.yml" up -d --no-build --wait
pc ps
```

Dieser Rollback nutzt die vorhandenen Volumes. Prüfe die Medien und Datenbank separat. Spiele Daten nur bei bestätigter Beschädigung aus der Sicherung in kontrollierte Ersatzvolumes ein und halte neuere Medien bis zur Klärung vor. Verwende für die Aktualisierung oder den Rollback keinen Befehl, der die aktiven Volumes löscht.

## Kurzer Live-Abnahmetest

1. Verwende einen Live-Teststream, dessen Aufnahme und Weiterverwendung erlaubt sind. Aktiviere zunächst nur diesen Kanal. Prüfe den erkannten Livestatus und dann die Aufnahme mit geschlossenem Browser. Erweitere den Test anschließend auf Twitch, YouTube und Kick und prüfe jeweils Auflösung, FPS und Bitrate gegen das tatsächliche Angebot.
2. Verursache zwei zulässige sichtbare Spielereignisse mit klar getrennten Zeitfenstern und einer Reaktion. Prüfe die gespeicherten Kandidaten, Ablehnungsgründe, Wartezeit und den Renderfortschritt. Produziere 429 nur mit einem Testprojekt oder einem Mock, indem du ein lokales Limit setzt und beobachtest, dass der Ausschnitt nach dem Wartefenster wieder geprüft wird.
3. Prüfe auf einem echten iPhone über HTTPS **Open on Twitch** im Hochformat und die optionale Vorschau bei ausreichender Breite. Schließe den Tab während der Aufnahme. Prüfe einen geplanten App-Neustart erst nach Sicherung und kontrolliere wiederhergestellte Zeitmarken, Clips und vorhandene Aufnahmen.
4. Prüfe den Exportclip auf sichtbaren Start, 1080 × 1920, Facecam, Rand-HUD und Tonbild-Synchronität. Prüfe getrennte Mikrofonspuren und einen gemischten Stream getrennt. Sende ausschließlich einen geprüften Buffer-Entwurf und kontrolliere durch den erneuten Aufruf, dass nur ein Entwurf vorhanden ist. Prüfe das Discord-Ergebnis und dokumentiere CPU, RAM, SSD-Reserve und die Phasenzeiten.

## Offline geprüft

Der alte PR #6 war bereits in `main` und hatte auf GitHub keine gemeldeten Checks. Seine 642 Prüfungen liefen erneut lokal. Der Offline-Vergleich verwendete ein synthetisches 1080p60-Video mit isolierter, zulässiger Sprachprobe. Aus 66 Sekunden wurden in 100,7 Sekunden ein 59,3-Sekunden-Clip in 1080 × 1920 und 206 erkannte Wörter. Der Prozess erreichte etwa 1,32 GiB RSS mit zwei zugewiesenen CPUs, einem Modell und einem Encoder-Thread und 2,75 GiB Adressraum pro Prozess. Der Benchmark traf die Qualitätsentscheidung zum Zurückhalten korrekt, weil nur ein Lautstärkefund vorlag. Die 27,9 Mbit/s des ultrafast-Testvideos bilden keine reale Provider-Bitrate ab. PostgreSQL und der App-Container wurden lokal mit isolierten Volumes neu gestartet. Ein gespeicherter Queue-Eintrag blieb erhalten. Der Browser-Test verwendet Chromium mit iPhone-Viewport und simuliert den Twitch-Player. Aggregierter Container-RAM, produktive Kontingente, native iOS-Safari-Wiedergabe und ein 24-Stunden-Lauf auf Shared-CPU-Hardware bleiben Teil der Live-Abnahme.
