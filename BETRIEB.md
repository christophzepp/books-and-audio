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
