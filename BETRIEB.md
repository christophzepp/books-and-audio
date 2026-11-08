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

Fortschrittsabgleich, Bibliothekserkennung und Dateiprüfung haben getrennte Abläufe. Unveränderte, bereits bestätigte E-Book-Stände werden nach Prüfung der aktuellen API- und Mountidentität ohne Öffnen der EPUB übernommen. Seit Beta 12 benötigen auch geänderte Lesestände und offene Übertragungsjournale bei unveränderter API-Dateiversion keine erneute Inhaltsprüfung. Aktuelle API-Identitäten und Fortschritte werden vor und nach Schreibvorgängen verglichen; eine konkurrierende Änderung verhindert den Abschluss des Journals.

Der Positionsindex liegt als versionsgebundene JSON-Datei unter `state/ebook-index-cache`, standardmäßig mit maximal 1 GiB und 4.096 Dateien sowie einem RAM-Cache für vier Indizes. Das gesamte `state`-Verzeichnis sollte auf einer SSD liegen. Es wird keine Kopie der Mediendatei angelegt. Fehlende oder beschädigte Indizes werden aus aktuell geprüften Originalbytes neu erstellt; bei unveränderter API-Dateiversion genügt der historische Dateinachweis auch für eine Fortschrittsübertragung. Die Cachedateien sind abgeleitete Daten, die Datenbank und Übertragungsjournale bleiben dagegen unverzichtbar.

Seit Beta 11 werden Medienordner gemeinsam geprüft und im unveränderten Leerlauf dauerhaft wiederverwendet. Vor tatsächlichen E-Book-Dateioperationen und manuellen Medienprüfungen wird der betroffene Ordnertyp erneut sicher geöffnet. Mount-, relevante Deployment- oder Konfigurationsänderungen entwerten den Prüfstand. Die GUI selbst löst keine Ordnerdurchläufe aus; unveränderte Darstellungsdateien werden nicht ständig neu geschrieben. Der Worker-Lebensnachweis wird weiterhin spätestens alle 60 Sekunden veröffentlicht. Die 150-Sekunden-Grenze für echte Deploymentnachweise bleibt bestehen.

Neue EPUB-Kandidaten werden zunächst anhand von Datei- und API-Metadaten beobachtet. Erst nach mindestens 60 stabilen Sekunden wird der Inhalt vollständig gelesen und indiziert. Bei Podcasts werden gültige negative Wartefristen vor lokalen Pfadprüfungen ausgewertet. Bereits bestätigte Podcast-Dateien und Audiofassungen verwenden ihre gebundenen Nachweise auch bei Statusänderungen. Neue oder laut API geänderte Medien sowie ausdrücklich angeforderte Audioprobes prüfen nur die betroffenen Dateien.

Optionale Einstellungen im bestehenden `config/config.json` (nur den Abschnitt ergänzen; die übrige Konfiguration erhalten):

```json
"io_policy": {
  "ebook_cache_max_bytes": 1073741824,
  "ebook_cache_max_entries": 4096,
  "ebook_verify_interval_seconds": 0,
  "ebook_discovery_interval_seconds": 300,
  "ebook_candidate_stable_seconds": 60,
  "ebook_retry_initial_seconds": 300,
  "ebook_retry_max_seconds": 86400,
  "folder_validation_interval_seconds": 0,
  "integrity_window_utc": null
}
```

`ebook_verify_interval_seconds: 0` bedeutet, dass es keine zusätzlichen regelmäßigen Inhaltsvollprüfungen gibt. Erstaufnahme und laut API geänderte Medien werden trotzdem physisch geprüft. Reine Fortschrittsänderungen und die Wiederaufnahme offener Journale verwenden aktuelle API-Prüfungen gegen vorhandene Nachweise. Für zusätzliche Hintergrundprüfungen kann ein Intervall ab 60 Sekunden und optional ein UTC-Stundenfenster, etwa `{"start_hour": 1, "end_hour": 3}`, gewählt werden. Das Fenster begrenzt ausschließlich zusätzliche Hintergrundprüfungen. Ein Dateiaustausch außerhalb der Mediendienste bei unveränderten API-Metadaten kann bis zu einer manuellen oder optionalen Integritätsprüfung unbemerkt bleiben; ein reiner Fortschrittstransfer löst keine erneute Inhaltsprüfung aus.

Unter `io_metrics` in `state/control/status.json` stehen Zähler für Inhaltslesungen und gelesene Bytes, Indexaufbauten und Cachetreffer, übersprungene E-Book-Abgleiche, Ordnerprüfungen und ausgelassene GUI-Schreibvorgänge. Sie zählen seit Erstellung der jeweiligen Worker-/Verwaltungsinstanz und werden nur im RAM geführt. Sie messen Programmoperationen, keine physischen Plattenzugriffe. Für die Abnahme im Livebetrieb zusätzlich den vorhandenen Festplattenwächter und Block-I/O-Zähler vergleichen, einschließlich indirekter Last durch API-Aufrufe bei den Originaldiensten. Scrub und andere Wartungsarbeiten getrennt auswerten.

Gezielte Regressionen:

```sh
python -m unittest test_ebook_io test_ebook_discovery test_folder_io_policy test_podcast_idle_io test_io_policy
```

## Dauerhafte Mediennachweise ab Beta 11

Das private `state`-Verzeichnis muss auf der NVMe/SSD liegen. Unter `state/media-observations` liegen begrenzte, abgeleitete JSON-Nachweise für Podcast-Dateien, native Hörbuch-/Mitlese-Ausgaben und Medienordner (maximal 128 MiB, 100.000 Einträge, 128 KiB je Eintrag). E-Books behalten ihren dauerhaften Positionsindex und einen gezielt gebundenen Prüfstand. Es werden keine Mediendateien kopiert. Diese Cacheeinträge sind keine Freigaben und keine Schreibberechtigung; Datenbanken, Schlüssel und Journale müssen weiterhin gesichert werden.

Unveränderte positive Nachweise haben **keine zeitliche Ablaufgrenze**. Sie überstehen Containerneustarts. Ein neuer Podcast löst nur Prüfungen seiner eigenen Dateien aus. Aktuelle API-Quellmetadaten, Folgen-/Dateiidentitäten, das persönliche Konto und die relevante Mountgeneration müssen zum gespeicherten Nachweis passen. Fremde Ordneränderungen oder Darstellungsnamen entwerten nicht den ganzen Bestand. Ein tatsächlicher Mountwechsel, eine Dateiänderung laut API, ein fehlender/beschädigter Eintrag oder eine fehlgeschlagene manuelle Prüfung betrifft den jeweiligen Nachweis. Ein Host-Neustart kann eine neue Mountprüfung benötigen.

`folder_validation_interval_seconds: 0` und `ebook_verify_interval_seconds: 0` deaktivieren zusätzliche regelmäßige physische Prüfungen. Bereits ausdrücklich konfigurierte positive Intervalle bleiben erhalten; für dauerhafte Wiederverwendung diese Werte auf `0` setzen. Manuelle Prüfungen lesen weiterhin die Originaldatei. Fortschrittsübertragungen prüfen seit Beta 12 aktuelle APIs gegen die historischen Mediennachweise. Wird eine Datei außerhalb der Mediendienste bei unveränderten API-Daten ersetzt, erkennt der Dienst dies erst bei einer manuellen oder zusätzlichen Integritätsprüfung. Historische Nachweise allein erteilen keine Schreibberechtigung; aktuelle Konten, Dateiversionen und Konfliktprüfungen müssen weiterhin passen.

Pro Durchlauf werden schreibgeschützte API-Sammelabfragen und Indizes aufgebaut. ABS liefert persönliche Hörstände einmal und jede Podcastserie einmal; PinePods liefert seine paginierten persönlichen Kataloge und GPodder-Aktionen je Feed; Jellyfin liefert den betreffenden Bibliotheksbestand. PinePods-Sammelmetadaten werden je Feed gegen eine vollständige native Antwort abgeglichen. Abweichende Formate verwenden einzelne geprüfte Abfragen. Neue Durchläufe lesen neue Hörstände, sodass Zurücksetzen und Weiterhören erkennbar bleiben. Seit Beta 13 entwerten Schreibvorgänge gezielt die betroffenen Teilnehmer und Folgen; unabhängige Katalogdaten bleiben im laufenden Beobachtungsdurchlauf erhalten. Frische Kontoprüfung, CAS, Übertragungsrücklesen und Wiederaufnahme verwenden die Originalclients.

Unter `io_metrics` stehen unter anderem `api_observation_requests`, `api_observation_cache_hits`, `api_observation_index_hits` und `native_observation_durable_hits`; unter `io_metrics.media` stehen `podcast_variant_source_checks`, `podcast_variant_source_cache_hits` und `podcast_variant_source_disk_hits`. Zähler starten beim Worker-Neustart neu. Nach einem ersten Prüf-/Migrationsdurchlauf mindestens mehrere unveränderte Zyklen und mehr als 15 Minuten beobachten; zusätzlich einen Worker-Neustart prüfen. Diese Programmzähler und Pfad-Syscalls unterscheiden sich von physischen Plattenzugriffen. Andere Mediendienste können die HDD weiterhin aufwecken.

Gezielte Regressionen:

```sh
python -m unittest test_podcast_durable_io test_folder_durable_io test_ebook_standby test_native_durable_io test_podcast_bulk_io
```

## Fortschrittsübertragungen ab Beta 12

Der physische Mediennachweis und die aktuelle Übertragungsprüfung sind getrennt. Ein gültiger Nachweis behält seinen ursprünglichen Prüfzeitpunkt. Vor und nach einem Schreibvorgang werden die aktuellen Konten, Quellen, Dateiversionen, Mountnachweise und Hör-/Lesestände über die Original-APIs verglichen. Eine reine Fortschrittsänderung löst weder lokale Pfadauflösung, Dateiattributprüfung, EPUB-Hash/Indexaufbau noch eine erneute Audio-Laufzeitmessung aus. Für native Hörbuch-/Mitlese-Ausgaben werden die ursprünglichen EPUB-Hashes ebenfalls wiederverwendet.

Fehlende oder beschädigte Nachweise und laut API neue Dateiversionen werden vor einer neuen Übertragung gezielt aufgebaut. Innerhalb eines bereits offenen Journals gibt es keinen stillen Rückweg zur physischen Dateiprüfung: Fehlt der passende Nachweis, wartet die Wiederaufnahme auf eine ausdrückliche Prüfung. Das Journal bleibt erhalten; unsichere Schreibvorgänge werden nicht wiederholt. Eine fehlgeschlagene ausdrückliche Dateiprüfung entwertet den betroffenen Nachweis, ohne Hörstände oder Freigaben zu löschen. Ein reiner Netzwerkfehler wird von einem bestätigten Dateifehler unterschieden.

Die Indexgrenzen sind über `ebook_cache_max_bytes` (1 MiB bis 16 GiB) und `ebook_cache_max_entries` (1 bis 100.000) konfigurierbar. Standardmäßig bleiben bis zu 4.096 Indizes innerhalb von 1 GiB erhalten; der RAM-Cache bleibt bei vier Indizes. Die tatsächliche Kapazität hängt von der EPUB-Komplexität ab. Ein explizit kleiner gesetztes Budget bleibt unverändert; bei Verdrängung benötigt die betroffene Ausgabe vor einer neuen Übertragung einen gezielten Neuaufbau.

Ein unbemerkter Austausch von Dateiinhalten außerhalb der Mediendienste bei unveränderten API-Metadaten wird bewusst nicht durch jeden Fortschrittsabgleich gesucht. Dafür dienen „Dateiprüfung“ und die optionalen Integritätsintervalle. Die frischen Host-Deploymentnachweise behalten ihre Grenze von 150 Sekunden; dies ist keine Ablaufzeit des Mediennachweises und verursacht allein keine Inhaltslesung.

Regressionen mit echten Koordinatoren und simulierten APIs verbieten Medienzugriffe während Fortschrittsänderungen, Abschließen, Zurücksetzen und Journalwiederaufnahme, auch nach einem Neustart. Sie prüfen weiterhin Konflikte, API-Versionwechsel, fehlende Nachweise und unveränderte Freigaben. Diese Tests belegen Programmverhalten; für physische HDD-Zugriffe zusätzlich Block-I/O und die anderen beteiligten Mediendienste beobachten.

```sh
python -m unittest test_podcast_transfer_io test_ebook_progress_standby test_audio_transfer_standby test_media_io_integration test_folder_transfer_io test_admin_media_guard test_epub_index_capacity
```

## Importprüfungen und größere Bestände ab Beta 13

Die automatische EPUB-Erkennung unterscheidet dauerhaft abgelehnte Inhalte von vorübergehenden Ausfällen. Kann eine stabile, eindeutig zugeordnete Datei inhaltlich nicht übernommen werden, bleibt der negative Prüfnachweis im privaten Zustand auf der NVMe erhalten, auch nach einem Neustart. Solange die aktuellen API-Dateiidentitäten, das Konto und die betreffende Mountzuordnung passen, wird diese Datei nicht erneut geöffnet oder indiziert. Eine fehlende aktuelle Konto- oder Mountbestätigung wird dadurch nicht übergangen. Änderungen an einer anderen Ausgabe entwerten ihren Nachweis nicht.

Eine Änderung der gebundenen Quell- oder Dateiversion laut API löst eine gezielte neue Prüfung aus. Die API-Bestandsabfrage bleibt regelmäßig; `ebook_discovery_interval_seconds` ist keine Ablaufzeit für die Inhaltsprüfung einer unveränderten, abgelehnten EPUB. Es gibt damit keine neue regelmäßige Vollprüfung des Medienbestands. Auch negative Nachweise können einen Dateiaustausch außerhalb der Mediendienste ohne geänderte API-Metadaten nicht automatisch erkennen.

Unter **Bücher → Offene E-Book-Importe** werden fehlgeschlagene Importe mit ihrem Grund angezeigt. **Diese Datei prüfen** wiederholt die Prüfung einer einzelnen Datei; **Offene Importe erneut prüfen** startet einen ausdrücklichen neuen Versuch für die offenen Kandidaten. Dabei kann die Medienplatte anlaufen. Eine gezielte Prüfung behält die Nachweise, Wartezeiten und sichtbaren Hinweise anderer Kandidaten bei. Mehrdeutige Dateiidentitäten bleiben gesperrt. Bereits vorhandene Zuordnungen, Hör-/Lesestände und Freigaben werden nicht zurückgesetzt.

Netzwerkfehler, vorübergehend fehlende Quellen oder nicht verfügbare Dateien gelten nicht als dauerhafte Inhaltsablehnung. Für sie wächst die Wiederholungswartezeit exponentiell, standardmäßig von **300 Sekunden bis höchstens 86.400 Sekunden**. Ein Neustart setzt diese Wartezeit nicht zurück. `ebook_retry_initial_seconds` erlaubt 30 bis 86.400 Sekunden; `ebook_retry_max_seconds` muss mindestens dem Anfangswert entsprechen und darf höchstens 604.800 Sekunden betragen. Die tatsächliche Ausführung erfolgt frühestens nach der Wartezeit im nächsten passenden Erkennungsdurchlauf. Die GUI zeigt den frühesten nächsten Versuch an und erlaubt einen bewussten Retry. Eine Inhaltsablehnung erhält dagegen keine automatische Wiederholungsfrist.

Für große Buchbestände werden die gespeicherten Zuordnungen, Revisionen und nativen Ausgaben einmal je Durchlauf für die ersten Beobachtungen validiert und indiziert. Vor einer tatsächlichen Übertragung und beim Wiederaufnehmen eines Journals werden weiterhin die aktuellen Originalzuordnungen, Pausen, Freigaben und Fortschritte geprüft. Eine Änderung während des Durchlaufs kann somit keinen veralteten Beobachtungsstand zur Schreibberechtigung machen.

Bei Podcasts bleiben unabhängige Bestandsdaten nach einer Übertragung erhalten. Der geänderte Hörstand wird gezielt entwertet und erneut gelesen, auch nach einer verlorenen Schreibantwort. Frische Prüfungen vor dem Schreiben und Rücklesekontrollen verwenden weiterhin die Originalclients. Interne Indizes vermeiden außerdem wiederholte vollständige Katalogkopien; herausgegebene Datensätze bleiben getrennte Kopien. Die Zahl der API-Abfragen hängt weiterhin von Serien, Katalogseiten, Teilnehmern und tatsächlichen Änderungen ab. Nicht jede Abfrage lässt sich unabhängig von der Bestandsgröße bündeln.

Die Wartung von `state/media-observations` und `state/ebook-index-cache` führt für normale neue Cacheeinträge keinen vollständigen Verzeichnisdurchlauf mehr aus. Eine begrenzte Verwaltung im RAM aktualisiert Größen und Verdrängungsreihenfolge; eine Verzeichnissperre stimmt mehrere schreibende Prozesse ab. Nach Neustarts, geänderten Speichergrenzen oder erkannten externen Verzeichnisänderungen wird diese Verwaltung neu aufgebaut. Cacheleser prüfen weiterhin ihre einzelnen Nachweise. Diese Wartung greift ausschließlich auf den privaten NVMe-Zustand zu, verändert keine Freigaben und verkürzt keine Mediennachweise durch einen Zeitablauf.

Gezielte Regressionen prüfen unter anderem dauerhafte Ablehnungen ohne Medienzugriff, API-Änderungen, gezielte Wiederholungen, Wartezeiten, große Zuordnungsbestände und Änderungen vor dem Schreiben:

```sh
python -m unittest test_ebook_discovery_standby test_admin_ebook_retry test_io_policy test_mapping_cycle_index test_audio_mapping_cas test_observation_scaling test_cache_maintenance
```
