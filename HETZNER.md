# PeakCut auf dem Hetzner-Testserver

Das Docker-Profil ist für Ubuntu mit 2 Shared vCPU, 4 GB RAM und 80 GB SSD ausgelegt. Es verwendet das vorhandene PeakCut, PostgreSQL und Caddy. Nur Caddy veröffentlicht Ports. HTTP leitet auf HTTPS um, und das Dashboard verlangt eine Anmeldung. Die Kontoerstellung erfolgt über die Serverkonsole. PeakCut erstellt in diesem Profil ausschließlich Buffer-Entwürfe und verarbeitet keine direkten Veröffentlichungsaufträge.

## 1. Voraussetzungen prüfen

```bash
docker version
docker compose version
df -h
```

Verwende Docker Engine mit Compose v2.24.4 oder neuer. Falls Docker fehlt, verwende die [offizielle Docker-Anleitung für Ubuntu](https://docs.docker.com/engine/install/ubuntu/). Weise deiner Domain einen DNS-A-Record mit der öffentlichen IP des Servers zu. Verwende einen AAAA-Record nur mit funktionierendem IPv6. Prüfe, dass TCP 80 und 443 für Caddy frei und über die vorhandene Hetzner- und Ubuntu-Firewall erreichbar sind. Caddy benötigt diese Ports für HTTPS und die Zertifikatsvalidierung. Prüfungen oder spätere Änderungen an den Firewallregeln erfolgen separat. Die folgenden Befehle ändern weder Firewall noch SSH.

## 2. Checkout und Secrets einrichten

```bash
git clone https://github.com/drerie222/peakcut.git
cd peakcut
git switch main
umask 077
cp -n .env.example .env
chmod 600 .env
sed -i 's/^# PEAKCUT_DOMAIN=peakcut.example.com$/PEAKCUT_DOMAIN=clips.example.org/' .env
if ! grep -q '^POSTGRES_PASSWORD=' .env; then
  printf '\nPOSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> .env
fi
${EDITOR:-nano} .env
```

Ersetze `clips.example.org` durch deine Domain. Bei einer vorhandenen Installation behältst du deren `.env`, Datenbankpasswort und Volumes bei. Das zufällige Hex-Passwort funktioniert direkt in der von Compose gebildeten Datenbank-URL. Halte `.env` außerhalb von Git und hänge sie an keine Support-Ausgaben an. Nutze für die Konfigurationsprüfung `config --quiet`, da ein reguläres `docker compose config` aufgelöste Secrets anzeigen kann.

Trage nach Bedarf diese Werte in `.env` ein. Leere Variablen lassen die jeweilige Integration inaktiv.

| Variable | Eintrag |
| --- | --- |
| `DISCORD_CLIPS_WEBHOOK_URL` | Webhook für **#clips** |
| `DISCORD_LIVE_WEBHOOK_URL` | Webhook für **#live-streams** |
| `DISCORD_STATUS_WEBHOOK_URL` | Webhook für **#status** |
| `BUFFER_API_KEY` | Persönlicher Buffer-API-Key mit Zugriff auf den verbundenen TikTok-Kanal |
| `BUFFER_TIKTOK_CHANNEL_ID` | ID dieses Kanals |

Buffer benötigt Lesezugriff auf Kanal und Beitrag sowie Schreibzugriff für Entwürfe. Die [Buffer-Einrichtung](README.md#save-a-tiktok-draft-in-buffer) beschreibt Kanalauswahl und Freigabe. **Check publication status** fragt bei Bedarf den Status des vorhandenen Beitrags bei Buffer ab. Der Status wird gespeichert. Manche Videoabrufe erfolgen zeitversetzt. Warte, bis Buffer das Video übernommen hat, bevor du den Link im PeakCut-Editor widerrufst. Der Link ist ein vertraulicher HTTPS-Link, den jeder mit der URL bis zum Widerruf abrufen kann.

## 3. Dienste starten und Administrator anlegen

```bash
pc() { docker compose -f docker-compose.yml -f deploy/docker-compose.hetzner.yml --profile own-https "$@"; }
pc config --quiet
pc up -d --build
pc ps
pc exec app python -m studio.admin create you@example.org
curl -fsS https://clips.example.org/healthz
```

Gib das Administratorpasswort interaktiv ein. Bei einem vorhandenen Konto kannst du es mit `pc exec app python -m studio.admin grant you@example.org` zum Administrator machen. Öffne dann `https://clips.example.org/app`. Der Modelldownload benötigt beim ersten Start eine Internetverbindung und schreibt rund 631 MiB in das PeakCut-Volume. App und PostgreSQL haben Healthchecks. Docker startet beendete Dienste automatisch neu. Ein dauerhaft fehlschlagender Healthcheck ist ein Anlass zur Diagnose mit `pc ps` und `pc logs --tail=100 app db`. Ein Healthcheck allein löst keinen Docker-Neustart aus.

Öffne **Watchlist**, importiere die 30 Streamer und aktiviere die Kanäle, für die Aufnahme und Weiterverwendung erlaubt sind. Die Einträge beginnen deaktiviert. Die Priorität A–C lässt sich in **Edit** ändern. Setze **Recordings** auf `1`. Aktiviere **Save TikTok drafts in Buffer** erst, wenn HTTPS und der verbundene Kanal eingerichtet sind. Vorhandene Clip-Funktionen wie der manuelle Download bleiben verfügbar.

## 4. Startwerte und Streamauswahl

| Bereich | Startwert |
| --- | --- |
| PeakCut / PostgreSQL / Caddy | 1,50 / 0,30 / 0,15 vCPU und 2.816 / 384 / 96 MiB RAM, ohne Container-Swap |
| Aufnahmen / lokale Modellkopien / FFmpeg-Encoder | Je 1, mit einem Modell- und einem Encoder-Thread. Analyse und Rendering teilen sich einen CPU-Arbeitsplatz. |
| Zusätzliche Aufträge | 8 wartende Videos, je 4 wartende manuelle oder Live-Renderaufträge. Volle Warteschlangen melden Überlastung oder bremsen die Live-Verarbeitung. |
| Video | 1080 × 1920 bei 24 fps. Maximal 2 Stunden je Upload-VOD und 6 Stunden am Stück ohne Aufnahmepartner. |
| Datenträger | 12 GB freie Reserve für den Daten- und den Render-Temp-Pfad, 168 Stunden Aufbewahrung für Rohdateien |
| Wechsel | 20 Minuten Mindestaufnahme, 10 Minuten Cooldown, nach 60 Minuten Wechsel, wenn ein geeigneter Stream wartet |

Die Watchlist fragt aktivierte Kanäle etwa alle zwei Minuten ab. Cache und Backoff vermeiden wiederholte Anfragen. Neue Plattformabfragen beginnen mindestens zwei Sekunden auseinander. Der Dashboard-Score zeigt für jeden beobachteten Kandidaten seine Faktoren und den Auswahlgrund. A/B/C gibt 30/15/0 Punkte. Eine GTA-VI-Kategorie gibt 30 Punkte. Bei fehlender Kategorie zählt ein passender Titel als unbestätigter Hinweis mit 10 Punkten. Hinzu kommen bis zu 60 Punkte für Wartezeit. Bisherige Aufnahmeminuten und fertige Clips aus den letzten 24 Stunden geben einen gedeckelten Abzug. Ab drei Stunden Wartezeit bekommen die am längsten wartenden Kandidaten die nächsten freien Plätze vor höheren Punktwerten. Plattform-Metadaten können veraltet oder falsch sein. Audio, Gameplay und Chat fließen erst in die Highlight-Analyse aufgenommener Streams ein.

Vor Ablauf der regulären Aufnahmezeit braucht ein Wechsel mindestens 25 Punkte Vorsprung bei zwei aufeinanderfolgenden Prüfungen. PeakCut beendet die Aufnahme kontrolliert, verarbeitet bereits angenommene Clips und wartet mit dem Start auf einen freien physischen Aufnahmeplatz. Bei ausgelastetem CPU/RAM oder vollem Datenträger können Wechsel länger dauern. Ein manuell gestoppter Watchlist-Stream wird für die laufende Sendung übersprungen. Für Installationen mit mehreren Aufnahmeplätzen sortiert derselbe Score die nächsten Starts. Die automatische Rotation ist für den Betrieb mit einem einzigen Platz vorgesehen. Relevante `PEAKCUT_WATCH_*`-Werte stehen in [`.env.example`](.env.example).

## 5. Speicher und Betrieb

```bash
pc ps
docker stats --no-stream
df -h
pc exec app df -h /data /tmp
```

Temporäre VOD-Audios und Renderdateien werden nach der Verarbeitung entfernt. Beim Aufräumen werden ausschließlich alte, von PeakCut erzeugte Live-Rohdateien gelöscht, sobald keine Aufnahme, Wiederherstellung oder ausstehende Bearbeitung mehr darauf zugreift. Der fertige Clip, Vorschaubild, Untertitel, Transkript, Buffer-Entwurf und gültige Videolink bleiben bestehen. Nach Ablauf der Rohdatenfrist kannst du den fertigen Clip weiterhin herunterladen. Ein neuer Schnitt aus demselben Rohmaterial entfällt. `PEAKCUT_RAW_RETENTION_HOURS=0` deaktiviert die automatische Aufbewahrungsfrist.

Unter der Reserve beendet PeakCut betroffene Aufnahmen und startet vorerst keine neuen Aufnahmen oder Renderings. Bereits gestartete Renderings nutzen die Reserve zum Abschließen. **#status** kann darüber per Discord informieren. Live-Aufnahmen, die technisch nicht Schritt halten und mehr als zwei Minuten ausstehendes Audio ansammeln, stoppen kontrolliert. Bereits angenommene Clips können weiterverarbeitet werden. Fertige Clips und hochgeladene Originale können die 80-GB-SSD langfristig füllen und müssen bewusst archiviert oder im Dashboard gelöscht werden. Prüfe vor dem Löschen oder Rendern einer neuen Version, ob noch ein Buffer-Entwurf auf die aktuelle Version zugreift.

Die Volumes `data`, `pgdata`, `caddy_data` und `caddy_config` sind persistent. Sichere insbesondere das Medien-Volume zusammen mit einem Datenbank-Dump, bevor du den Server neu aufsetzt oder Daten verschiebst. Einen Dump kannst du vom Server aus ohne veröffentlichten Datenbankport erzeugen.

```bash
umask 077
pc exec -T db pg_dump -U peakcut -Fc peakcut > peakcut-postgres.dump
```

## Teststand und Engpässe

- `python tests/run.py` bestand alle **623 Prüfungen in 25 Testgruppen**. Die Suite prüft unter anderem Auswahl, Wechsel, Warteschlangen, Speicherbereinigung und simulierte Plattform-, Discord- und Buffer-Antworten. Der Gaming-Test prüft auch einen Clip über 60 Sekunden.
- INT8-Parakeet mit einer Modellkopie und einem Thread belegte nach der zweiten Dekodierung rund **0,91 GiB Prozess-RAM**. Ein Offline-Durchlauf mit `python tests/benchmark_small.py` nutzte dieses echte Sprachmodell und FFmpeg. In **57,9 s** entstanden aus 66 s synthetischem Material mit wiederholter Sprachprobe **59,3 s bei 1080 × 1920** mit Untertiteln. PeakCuts höchster Prozess-RSS lag bei **1,33 GiB**. Der Cloud-Container hatte ein Adressraum-Limit von 2,75 GiB je Prozess; seine Umgebung wies weiterhin Cloud-weite Cgroup-Werte aus. Die echten Hetzner-Ressourcen, das aggregierte Container-RAM-Limit und ein Dauerlauf mit echten Plattformstreams wurden nicht getestet.
- PostgreSQL und PeakCut erreichten im Compose-Test `healthy`. Die aufgelöste Konfiguration veröffentlichte nur die Caddy-Ports 80 und 443. Ein öffentlicher TLS-Handshake benötigt deine DNS-Domain und wird bei der Installation geprüft.
- Shared-CPU-Auslastung, gleichzeitige Downloads und hochauflösende Eingaben erhöhen die Bearbeitungszeit. Beobachte freie RAM- und Datenträgerreserve, Überlastungsmeldungen und den Umfang der fertigen Dateien. Bei dauerhaftem Rückstand kann eine niedrigere `PEAKCUT_OUT_FPS`-Einstellung den Renderaufwand senken. Die bestehende lokale Lautstärkeauswahl funktioniert ohne externen AI-Key. Automatische Highlight-Titel und semantische Auswahl benötigen die bereits unterstützte, von dir konfigurierte KI.
