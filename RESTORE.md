# Sicherungen von Books & Audio und dem gPodder-Relay prüfen

Die Wiederherstellungsprobe öffnet die Konten, Freigaben, Fortschrittsdaten und den letzten bestätigten gPodder-Abruf aus einem Sicherungspaar. Sie startet das Relay auf einer temporären Kopie seiner Datenbank. Dabei vergleicht sie den Inhalt des Verlaufs vor und nach dem Start, einschließlich bereits bestätigter und noch auszuliefernder Aktionen. Die Original-Apps können während der Probe weiterlaufen.

## Die Sicherungen auswählen

Verwende zwei abgeschlossene Sicherungen derselben Installation und desselben PinePods-Kontos. Bei den Sicherungen vor einem Dockge-Update enthält der Ordner des Hauptdienstes `stack-before.tar.gz` und `manifest.json`. Der separate Relay-Ordner enthält `config.json`, `state/relay.sqlite` und bei nativem PinePods-Tracking die Schlüsseldatei aus `native_source.api_key_file`, beispielsweise `pinepods-api.key`. Etwaige SQLite-WAL- und Journaldateien gehören zur selben kalten Sicherung.

Auch ein einzelnes TAR-Archiv des gestoppten Hauptstacks ist als `--main` zulässig. Bei benannten Dockge-Volumes müssen die obersten Archivverzeichnisse `config/`, `web_key/`, `state/`, `control/` und `web/` heißen. Die Probe vergleicht die beiden gesicherten Schlüsselkopien. Bei Bind-Mounts erwartet sie `config/`, `state/` sowie die Webanmeldung unter `web/web-auth/` oder `state/web-auth/`. Ein separates Archiv ohne Manifest erlaubt Struktur- und Entschlüsselungsprüfungen. Der Abgleich mit der bei der Sicherung aufgezeichneten SHA-256-Prüfsumme erfordert das Manifest.

## Vom Docker-Host ausführen

Setze die beiden Pfade auf die tatsächlichen Sicherungsordner des gewählten Zeitpunkts. Im Beispiel benötigt UID 1000 Lesezugriff auf beide Sicherungen. `--mount` meldet einen fehlenden Quellpfad als Fehler. Als temporären Arbeitsbereich verwendet der Container ein eigenes Dateisystem im Arbeitsspeicher. Dessen Größe muss für die ausgewählten Datenbanken und den SQLite-Arbeitsbereich ausreichen.

```sh
MAIN=/pfad/zur/sicherung/main-20261125
RELAY=/pfad/zur/sicherung/relay-20261125
IMAGE=ghcr.io/christophzepp/books-and-audio:0.1.0-beta.18

docker run --rm --read-only --network none --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=1g \
  --mount "type=bind,source=$MAIN,target=/main,readonly" \
  --mount "type=bind,source=$RELAY,target=/relay,readonly" \
  "$IMAGE" python restore_drill.py --main /main --relay /relay --json
```

Verwendet der Hauptdienst für dieselbe PinePods-Instanz eine interne Adresse und das Relay eine andere, gib das geprüfte Paar in dieser Reihenfolge an, etwa `--pinepods-origins http://pinepods:8055 https://pinepods.example.org`. Beide angegebenen Adressen müssen den jeweiligen Sicherungen entsprechen. Bei verschiedenen Kontonamen schlägt die Probe fehl.

## Das letzte Ergebnis in der Übersicht anzeigen

Lege einen Ordner `restore-check` im Steuerverzeichnis des Hauptdienstes an. Bei einem Dateisystem-Bind ist das typischerweise `…/state/control/restore-check`. Bei der öffentlichen Dockge-Vorlage entspricht es dem Unterordner `restore-check` im Volume, das im Webcontainer nach `/admin-state/control` eingebunden ist. Die tatsächliche Quelle lässt sich mit `docker inspect` und bei einem benannten Volume mit `docker volume inspect` ermitteln. Die Probe muss die Ergebnisdatei als UID des Webdienstes schreiben können.

Ergänze den Docker-Aufruf vor `"$IMAGE"` um `--mount "type=bind,source=$RESULT,target=/result"`. Setze `RESULT` auf den absoluten Pfad des vorbereiteten Ergebnisordners. Hänge an den Programmaufruf `--report-json /result/last.json` an. Die Probe ersetzt die vorherige Ergebnisdatei atomar, auch wenn ein Prüfschritt fehlschlägt. Webdienst und Worker brauchen nur ihren üblichen Zugriff auf das Steuerverzeichnis.

## Ergebnis lesen

| Prüfschritt | Was die Probe verifiziert |
| --- | --- |
| `main_backup` | Lesbares Archiv, geschützte Ablagepfade, archivierte Schlüsselkopien bei Volume-Sicherungen und die Prüfsummen des mitgelieferten Manifests. |
| `accounts` | Das Webkonto ist initialisiert. Der gesicherte Schlüssel entschlüsselt Basiszugänge, Verbindungsprofile und vorhandene verschlüsselte Verwaltungsaufträge. |
| `approvals_progress` | Die Ordnerzuordnungen sowie die aktiven und separat gespeicherten BookOrbit-Kontodatenbanken sind lesbar. SQLite-Integrität, Fremdschlüssel, Zustandswerte, Übertragungsaufträge und gespeicherte Zuordnungsfreigaben werden geprüft. |
| `relay_backup` | Relay-Konfiguration und Datenbank lassen sich wiederherstellen. Bei nativem Tracking ist die konfigurierte API-Schlüsseldatei lesbar. |
| `relay_cursor` | Das Relay kann mit seinem bisherigen Abrufstand starten. Baseline, Konto, Folgen, Journal, bereits bestätigte Ereignisse und noch offene Aktionen passen zusammen. |
| `account_match` | Das aktive PinePods-Profil entspricht dem gesicherten Relay-Konto und der angegebenen Instanz. |

`ok: true` bezeichnet einen bestandenen Versuch. `cursor` ist der letzte vom Relay bestätigte gPodder-Abrufstand. `confirmed` zählt gespeicherte Bestätigungen, `pending_actions` die Aktionen in der offenen Antwort an den Client. Insgesamt können mehrere Zustands-, Freigabe- oder Journalzeilen zu derselben Folge gehören. Bei einem Fehler nennt `error` den ersten fehlgeschlagenen Prüfschritt. Die folgenden Schritte wurden dann übersprungen. Exitcode 0 bedeutet bestanden, 1 fehlgeschlagen und 2 einen Aufruf- oder Veröffentlichungsfehler.

Das Ergebnis bezieht sich auf den Zeitpunkt der jeweiligen Sicherung. Ob Floppy selbst eine vom Relay quittierte Folge als gehört verbucht hat, lässt sich aus seiner eigenen Historie belegen. Die Probe führt keine Abrufe bei Floppy oder PinePods aus.
