# Books & Audio

**Öffentliche Beta · v0.1.0-beta.12**

Unter **Podcasts → Einstellungen → Primäre Podcastquelle** kannst du PinePods, Audiobookshelf oder Jellyfin bevorzugen. Bei widersprüchlichen Anfangsständen oder gleichzeitig geänderten Hörständen wird der vorhandene, geänderte Stand dieser Quelle übernommen. Eindeutige neue Änderungen anderer Apps werden weiterhin synchronisiert. Ohne Auswahl bleibt die bisherige Konfliktprüfung aktiv; ein Update aktiviert keine Bevorzugung. Die Einstellung synchronisiert keine Abonnements und ersetzt keine eindeutige Folgenzuordnung. BookOrbit-Podcasts bleiben vorbereitet und inaktiv.

Bei verschiedenen Audiofassungen gilt die Auswahl auch für den getrennten Statusabgleich. Sekunden werden dabei nicht zwischen den Fassungen umgerechnet. Ein fehlender Stand oder eine anfangs ungehörte primäre Quelle löscht keinen vorhandenen Fortschritt. Bewusstes Zurücksetzen wird erst nach einer bereits beobachteten Ausgangslage übertragen; einzelne manuelle Freigaben haben Vorrang. Ein Wechsel der Quelle wird zuerst geprüft und ist bei offenen Übertragungen gesperrt.

Eine bestätigte Freigabe des PinePods-Status „angefangen“ bleibt gültig, wenn sich beim Weiterhören nur die PinePods-Sekunden und deren abgeleitete Revisionen ändern. Die Statuswerte aller Teilnehmer, deren Zuordnung und die Audiofassung müssen gleich bleiben. Vor und während einer Übertragung werden die aktuellen Hörstände erneut kontrolliert.

Auf dem Handy ein Hörbuch unterbrechen und in einem anderen Player an derselben Stelle weiterhören: Books & Audio gleicht Hörpositionen, Lesefortschritt und den Gehört-Status von Podcastfolgen zwischen selbst gehosteten Mediendiensten ab.

| Medien | Was abgeglichen wird |
| --- | --- |
| Hörbücher | Hörposition und Abschlussstatus zwischen Audiobookshelf, dem Zuordnungsdienst BookOrbit oder BookBridge und optional Jellyfin. |
| E-Books | Lesestände für unterstützte, eindeutig zugeordnete EPUB-Ausgaben. Bei kompatiblen Mitlese-Ausgaben können Text- und Hörposition zusammengeführt werden. |
| Podcasts | Hörstand je Folge zwischen Audiobookshelf und Jellyfin; PinePods und GPodder können zusätzlich teilnehmen. Bei unterschiedlich langen Fassungen lassen sich „gehört“ und „ungehört“ getrennt von einer sekundengenauen Hörposition übernehmen. |

Die deutsche Weboberfläche zeigt zuerst Titel mit Handlungsbedarf. Bücher, Hörbücher und Podcasts haben eigene Listen. Unter **Freigaben** siehst du die gemeldeten Stände, kannst Konflikte prüfen und einzelne Folgen als gehört markieren oder auf Anfang setzen. Du kannst den gesamten Abgleich pausieren und fortsetzen. Eine globale Freigabe hebt keine ungeklärte Zuordnung einzelner Titel auf.

## Direkt mit Dockge installieren

Füge die [fertige Compose-Datei](compose.dockge.yaml) in einen neuen Dockge-Stack ein, setze `BOOKS_AUDIO_HOST` auf den Namen oder die IP deines Servers und wähle **Deploy**. Dockge lädt das fertige Image `ghcr.io/christophzepp/books-and-audio:beta` für AMD64 oder ARM64. Spätere Versionen installierst du über **Update** im selben Stack. Die [Dockge-Anleitung](DOCKGE.md) erklärt Anmeldung, Datenspeicherung und Versionswahl.

## Mit Docker ausprobieren

Du brauchst Docker mit Compose v2. Diese Schritte starten eine **leere, pausierte Instanz** auf dem eigenen Rechner. Es sind weder Medienkonten noch Beispieldaten hinterlegt.

```sh
git clone https://github.com/christophzepp/books-and-audio.git
cd books-and-audio
export APP_UID="$(id -u)" APP_GID="$(id -g)"
mkdir -m 700 runtime
docker compose run --build --rm init
docker compose up -d web worker
docker compose exec web cat /admin-state/web-auth/setup-token
```

Öffne **[localhost:8790](http://localhost:8790/)**. Mit dem einmaligen Einrichtungscode aus der letzten Zeile legst du dein Administratorkonto an. Der Code wird nach der Einrichtung ungültig. Läuft Docker auf einem anderen Rechner, öffne einen SSH-Tunnel zum Serverport 8790. Bei einer lokalen Portkollision kannst du vor dem Start z. B. `export WEB_PORT=8791` setzen.

Zum Beenden: `docker compose down`. Beim nächsten Start genügen dieselben Umgebungsvariablen und `docker compose up -d web worker`. Den Initialisierungsschritt nur für ein neues, leeres Datenverzeichnis ausführen.

## Eigene Bibliotheken verbinden

Der Vorschau-Stack enthält nur Books & Audio. Für echte Bibliotheken braucht die Beta eine zur Installation passende Basiskonfiguration, lesbare Medienpfade und eine regelmäßige Prüfung der beteiligten Container. Der Einrichtungsstand dieser Beta und die Schritte für Docker bzw. Dockge stehen in **[BETRIEB.md](BETRIEB.md)**.

## Entwicklung

Python 3.13, Requests und Cryptography; die Weboberfläche besteht aus HTML, CSS und JavaScript ohne Buildschritt. Für die Audioprüfung wird `ffprobe` verwendet (im Dockerfile enthalten).

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover
```

Die Tests verwenden synthetische Bibliotheken und simulierte Dienstantworten. Für Fehlerberichte sind Dienstversionen, Medium und die Meldung aus der Oberfläche hilfreich. Zugangsdaten und persönliche Bibliotheksdaten bitte weglassen.

Ab Beta 12 verwenden auch tatsächliche Fortschrittsübertragungen bereits bestätigte Mediennachweise und EPUB-Positionsindizes auf der NVMe. Eine reine Änderung von Hör- oder Lesestand öffnet unveränderte Mediendateien nicht erneut. Aktuelle Konten, API-Dateiversionen, Mountzuordnungen und Fortschritte werden weiterhin geprüft; konkurrierende Änderungen und unklare Übertragungen bleiben geschützt. Neue oder laut API geänderte Medien werden gezielt geprüft; manuelle Dateiprüfungen lesen weiterhin die Originaldatei.

Die gespeicherten Nachweise haben keine zeitliche Ablaufgrenze. Ein außerhalb der Mediendienste vorgenommener Dateiaustausch ohne Änderung der API-Metadaten wird erst bei einer ausdrücklichen oder optional zeitgesteuerten Dateiprüfung erkannt. Details, Speichergrenzen und Messung: [Fortschrittsübertragungen ab Beta 12](BETRIEB.md#fortschrittsübertragungen-ab-beta-12).

### Podcast-Tracking mit originalem Floppy

Der optionale private GPodder-Trackingdienst übernimmt neue PinePods-Hörereignisse mit dauerhaftem Importstand. Er schützt auch bei erneuten Vollanfragen des Original-Importers vor einer Wiederholung der gesamten Historie. Anleitung und Datenfluss: [TRACKING.md](TRACKING.md), Compose-Vorlage: [compose.tracking.yaml](compose.tracking.yaml).
