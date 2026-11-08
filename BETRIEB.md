# Eine eigene Installation anbinden

Die Vorschau aus der README benötigt keine anderen Dienste. Ein vollständiger Abgleich greift dagegen auf vorhandene Bibliotheken und Konten zu. In dieser Beta werden ihre Identitäten und die erlaubten Medienpfade bei der Ersteinrichtung festgelegt. Eine universelle automatische Erkennung beliebiger Docker-Stacks ist noch nicht enthalten.

## Erste Einrichtung für echte Medien

1. Sichere die vorhandenen Fortschrittsdaten deiner Dienste. Verwende für den ersten Test eine kleine Bibliothek. Für die neue Instanz ein **eigenes leeres Verzeichnis** wählen, z. B. `export DATA_DIR=./runtime-live` und `mkdir -m 700 "$DATA_DIR"`. Die Vorschau und produktive Instanz dürfen nicht gleichzeitig denselben Port oder Projekt-Namen beanspruchen.
2. Kopiere [config.example.json](config.example.json) außerhalb des öffentlichen Quellbaums. Das Beispiel zeigt den Audiobookshelf-/BookOrbit-Modus. Ersetze Adressen, beide Bibliothekskennungen, die Softwareversionen, die Medienwurzel und die beiden Docker-Image-IDs. Die Image-ID eines laufenden Dienstes liefert z. B. `docker inspect --format '{{.Image}}' audiobookshelf`. Der Basisprüfer erwartet die Containernamen `audiobookshelf` und `bookorbit` und denselben Bind-Mount unter `/audiobooks`. Er prüft in diesem Modus auch, ob der Container `bookbridge` konkurrierende Zuordnungen besitzt. Ohne passenden Prüfnachweis bleibt der Abgleich gesperrt.
3. Exportiere `APP_UID` und `APP_GID` wie in der README. Initialisiere die Instanz im Projektverzeichnis mit der angepassten Datei (absoluten Pfad einsetzen):

   ```sh
   docker compose run --build --rm -v /absoluter/pfad/basis.json:/basis.json:ro init python bootstrap.py /runtime --config /basis.json
   ```

   Die Abfrage der Dienstzugänge erfolgt verdeckt im Terminal. Schlüssel und verschlüsselte Zugangsdaten entstehen nur im privaten Datenverzeichnis. Der Administrator der Weboberfläche ist ein eigenes Konto.
4. Verbinde den Worker über eine lokale `compose.override.yaml` mit dem Docker-Netz der Medienserver bzw. verwende intern erreichbare Adressen. `localhost` im Worker verweist auf den Worker selbst. Binde benötigte Medienverzeichnisse beim Worker **nur lesend** ein. Die Pfade müssen dieselben Dateien bezeichnen wie in den Mediendiensten. Für zusätzliche Teilnehmer wie Jellyfin oder PinePods brauchst du neben einer Verbindung ggf. eigene Bibliotheks-, Ordner- und Containerzuordnungen. Beispiele für die geprüften Konfigurationsstrukturen enthalten [test_podcast_deployment.py](test_podcast_deployment.py) und [test_folder_deployment.py](test_folder_deployment.py).
5. Führe auf dem **Docker-Host** regelmäßig `python3 watch_deployment.py --root /absoluter/pfad/zum/runtime-live` aus, etwa einmal pro Minute per Cron. Der Prozess benötigt Zugriff auf die Docker-CLI und das private Datenverzeichnis; verwende dieselbe Benutzerkennung wie beim Worker. Die Prüfung schreibt den Nachweis in `state/deployment.json`. Der Worker akzeptiert ihn höchstens 150 Sekunden lang. Einen Docker-Socket im Webcontainer braucht die Anwendung nicht.
6. Starte Weboberfläche und Worker mit `docker compose up -d web worker`. Richte das Administratorkonto über den Einrichtungscode ein. Prüfe unter **Verbindungen** und **Medienordner** die Zuordnungen. Erst danach **Alle Synchronisationen freigeben** wählen. Uneindeutige Titel bleiben einzeln prüfbar.

**Dockge mit fertigem Image:** [Installation und Updates](DOCKGE.md).

**Eigener Build in Dockge:** Lege den Quellbaum samt Dockerfile und Compose-Datei in einen eigenen Stack-Ordner. Initialisiere das Datenverzeichnis wie oben. Trage `APP_UID`, `APP_GID` und bei abweichendem Speicherort `DATA_DIR` auch für spätere Starts in die Stack-Umgebung ein. Starte den Stack erst nach der Initialisierung. Bei einer bestehenden Installation ist diese Beta kein Ersatz für deren vorhandene Konfiguration.

**Sicherung:** Das gesamte private Datenverzeichnis bei gestopptem Stack sichern. Es enthält Schlüssel, verschlüsselte Profile, Datenbanken, Freigaben und Kontozustand; der Schlüssel allein oder die Datenbank allein genügt nicht zur Wiederherstellung.

## Medienzugriffe ab Beta 9

Fortschrittsabgleich, Bibliothekserkennung und Dateiprüfung haben getrennte Abläufe. Unveränderte, bereits bestätigte E-Book-Stände werden nach Prüfung der aktuellen API- und Mountidentität ohne Öffnen der EPUB übernommen. Ein offenes Übertragungsjournal, geänderte Stände oder Identitäten und eine fällige zusätzliche Integritätsprüfung verwenden diesen schnellen Rückweg nicht. Vor echten Übertragungen werden die Originalbytes weiterhin vollständig geprüft; eine Änderung während der letzten Anfrage verhindert den Abschluss des Journals.

Der Positionsindex liegt als versionsgebundene JSON-Datei unter `state/ebook-index-cache`, mit maximal 64 MiB und 64 Dateien sowie einem kleinen RAM-Cache. Das gesamte `state`-Verzeichnis sollte auf einer SSD liegen. Es wird keine Kopie der Mediendatei angelegt. Fehlende oder beschädigte Indizes werden aus aktuell geprüften Originalbytes neu erstellt; ein Cachetreffer ersetzt keine Dateiprüfung für eine Übertragung. Die Cachedateien sind abgeleitete Daten, die Datenbank und Übertragungsjournale bleiben dagegen unverzichtbar.

Seit Beta 11 werden Medienordner gemeinsam geprüft und im unveränderten Leerlauf dauerhaft wiederverwendet. Vor tatsächlichen E-Book-Dateioperationen und manuellen Medienprüfungen wird der betroffene Ordnertyp erneut sicher geöffnet. Mount-, relevante Deployment- oder Konfigurationsänderungen entwerten den Prüfstand. Die GUI selbst löst keine Ordnerdurchläufe aus; unveränderte Darstellungsdateien werden nicht ständig neu geschrieben. Der Worker-Lebensnachweis wird weiterhin spätestens alle 60 Sekunden veröffentlicht. Die 150-Sekunden-Grenze für echte Deploymentnachweise bleibt bestehen.

Neue EPUB-Kandidaten werden zunächst anhand von Datei- und API-Metadaten beobachtet. Erst nach mindestens 60 stabilen Sekunden wird der Inhalt vollständig gelesen und indiziert. Bei Podcasts werden gültige negative Wartefristen vor lokalen Pfadprüfungen ausgewertet. Positive Bereitschaft, fällige Audioprobes und echte Änderungen prüfen die lokalen Dateien weiterhin; Werbevarianten und Messnachweise behalten ihre bisherigen Gültigkeitsgrenzen.

Optionale Einstellungen im bestehenden `config/config.json` (nur den Abschnitt ergänzen; die übrige Konfiguration erhalten):

```json
"io_policy": {
  "ebook_cache_max_bytes": 67108864,
  "ebook_verify_interval_seconds": 0,
  "ebook_discovery_interval_seconds": 300,
  "ebook_candidate_stable_seconds": 60,
  "folder_validation_interval_seconds": 0,
  "integrity_window_utc": null
}
```

`ebook_verify_interval_seconds: 0` bedeutet, dass es keine zusätzlichen regelmäßigen Inhaltsvollprüfungen gibt. Erstaufnahme, echte Änderungen, offene Journale und Übertragungen werden trotzdem geprüft. Für zusätzliche Hintergrundprüfungen kann ein Intervall ab 60 Sekunden und optional ein UTC-Stundenfenster, etwa `{"start_hour": 1, "end_hour": 3}`, gewählt werden. Das Fenster begrenzt ausschließlich zusätzliche Hintergrundprüfungen, niemals die nötige Prüfung vor einer Übertragung. Ohne Medienbedarf kann eine externe Dateiänderung daher bis zur nächsten tatsächlichen Verwendung oder optionalen Integritätsprüfung unbemerkt bleiben; sie darf keinen ungeprüften Fortschrittstransfer auslösen.

Unter `io_metrics` in `state/control/status.json` stehen Zähler für Inhaltslesungen und gelesene Bytes, Indexaufbauten und Cachetreffer, übersprungene E-Book-Abgleiche, Ordnerprüfungen und ausgelassene GUI-Schreibvorgänge. Sie zählen seit Erstellung der jeweiligen Worker-/Verwaltungsinstanz und werden nur im RAM geführt. Sie messen Programmoperationen, keine physischen Plattenzugriffe. Für die Abnahme im Livebetrieb zusätzlich den vorhandenen Festplattenwächter und Block-I/O-Zähler vergleichen, einschließlich indirekter Last durch API-Aufrufe bei den Originaldiensten. Scrub und andere Wartungsarbeiten getrennt auswerten.

Gezielte Regressionen:

```sh
python -m unittest test_ebook_io test_ebook_discovery test_folder_io_policy test_podcast_idle_io test_io_policy
```

## Dauerhafte Mediennachweise ab Beta 11

Das private `state`-Verzeichnis muss auf der NVMe/SSD liegen. Unter `state/media-observations` liegen begrenzte, abgeleitete JSON-Nachweise für Podcast-Dateien, native Hörbuch-/Mitlese-Ausgaben und Medienordner (maximal 128 MiB, 100.000 Einträge, 128 KiB je Eintrag). E-Books behalten ihren dauerhaften Positionsindex und einen gezielt gebundenen Prüfstand. Es werden keine Mediendateien kopiert. Diese Cacheeinträge sind keine Freigaben und keine Schreibberechtigung; Datenbanken, Schlüssel und Journale müssen weiterhin gesichert werden.

Unveränderte positive Nachweise haben **keine zeitliche Ablaufgrenze**. Sie überstehen Containerneustarts. Ein neuer Podcast löst nur Prüfungen seiner eigenen Dateien aus. Aktuelle API-Quellmetadaten, Folgen-/Dateiidentitäten, das persönliche Konto und die relevante Mountgeneration müssen zum gespeicherten Nachweis passen. Fremde Ordneränderungen oder Darstellungsnamen entwerten nicht den ganzen Bestand. Ein tatsächlicher Mountwechsel, eine Dateiänderung laut API, ein fehlender/beschädigter Eintrag oder eine fehlgeschlagene manuelle Prüfung betrifft den jeweiligen Nachweis. Ein Host-Neustart kann eine neue Mountprüfung benötigen.

`folder_validation_interval_seconds: 0` und `ebook_verify_interval_seconds: 0` deaktivieren zusätzliche regelmäßige physische Prüfungen. Bereits ausdrücklich konfigurierte positive Intervalle bleiben erhalten; für dauerhafte Wiederverwendung diese Werte auf `0` setzen. Manuelle Prüfungen und echte Übertragungen bleiben aktuell geprüft. Wird eine Datei außerhalb der Mediendienste bei unveränderten API-Daten ersetzt, erkennt die unveränderte Beobachtung das erst bei einer solchen Verwendung oder zusätzlichen Integritätsprüfung. Ein historischer Cachetreffer darf keinen ungeprüften Transfer freigeben.

Pro Durchlauf werden schreibgeschützte API-Sammelabfragen und Indizes aufgebaut. ABS liefert persönliche Hörstände einmal und jede Podcastserie einmal; PinePods liefert seine paginierten persönlichen Kataloge und GPodder-Aktionen je Feed; Jellyfin liefert den betreffenden Bibliotheksbestand. PinePods-Sammelmetadaten werden je Feed gegen eine vollständige native Antwort abgeglichen. Abweichende Formate verwenden einzelne geprüfte Abfragen. Neue Durchläufe lesen neue Hörstände, sodass Zurücksetzen und Weiterhören erkennbar bleiben. Nach Schreibvorgängen werden die Beobachtungssnapshots verworfen. Frische Kontoprüfung, CAS, Übertragungsrücklesen und Wiederaufnahme verwenden die Originalclients.

Unter `io_metrics` stehen unter anderem `api_observation_requests`, `api_observation_cache_hits`, `api_observation_index_hits` und `native_observation_durable_hits`; unter `io_metrics.media` stehen `podcast_variant_source_checks`, `podcast_variant_source_cache_hits` und `podcast_variant_source_disk_hits`. Zähler starten beim Worker-Neustart neu. Nach einem ersten Prüf-/Migrationsdurchlauf mindestens mehrere unveränderte Zyklen und mehr als 15 Minuten beobachten; zusätzlich einen Worker-Neustart prüfen. Diese Programmzähler und Pfad-Syscalls unterscheiden sich von physischen Plattenzugriffen. Andere Mediendienste können die HDD weiterhin aufwecken.

Gezielte Regressionen:

```sh
python -m unittest test_podcast_durable_io test_folder_durable_io test_ebook_standby test_native_durable_io test_podcast_bulk_io
```
