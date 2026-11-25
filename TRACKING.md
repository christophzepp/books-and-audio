# PinePods, Audiobookshelf und Floppy

PinePods ist die bevorzugte Hörplattform. In Books & Audio lässt sich die primäre Podcastquelle unter **Podcasts → Einstellungen** auswählen. Bei echten Konflikten gewinnt deren geänderter Stand. Ein neues Weiterhören in der Backup-App wird trotzdem übernommen. Fehlende Daten werden nicht als Zurücksetzen gewertet.

Audiobookshelf lädt die Dateien und stellt sie Jellyfin bereit. Bei derselben Audiofassung werden Sekunden und Abschlussstatus übertragen. Bei bestätigten unterschiedlichen Fassungen überträgt Books & Audio nur angefangen/abgeschlossen. Es skaliert keine Hörpositionen zwischen Werbe- oder Premiumfassungen. Folgenkennungen, bestätigte Adresswechsel und Audiofassungen bleiben getrennte Prüfungen. BookOrbit bleibt bis zu einer geprüften originalen Podcast-Schnittstelle inaktiv.

## Originales Floppy mit inkrementellem Import

Floppys ursprünglicher GPodder-Importer fordert in regelmäßigen Abständen erneut die gesamte Historie an. Der optionale Books-&-Audio-Trackingdienst hält dafür einen dauerhaften Importstand vor. PinePods bleibt die Quelle; der Dienst stellt die Ereignisse über die originale GPodder-Schnittstelle für Floppy bereit. PinePods, Audiobookshelf und Floppy werden nicht umgebaut. Floppy schreibt über diesen Dienst keine Wiedergabeaktionen zurück.

Ab Beta7 gibt es zwei ausdrücklich gewählte Betriebsarten:

- Mit `native_source` liest der Dienst den nativen Abschlussstatus und Hörfortschritt des persönlichen PinePods-Kontos. Diese Schnittstelle ist für PinePods **0.9.0** geprüft. Die native Oberfläche erzeugt nicht für jede Änderung einen GPodder-Aktionseintrag; der native Stand ist deshalb die Quelle für den empfohlenen Modus.
- Ohne `native_source` bleibt der bisherige GPodder-Aktionsmodus bestehen. Er berücksichtigt die vorhandene Baseline und `exclude_devices`. Ein Image-Update allein aktiviert keine native Migration und behebt diese Lücke des Aktionsprotokolls nicht.

## Native Quelle einrichten

1. Vorhandene Floppy-Datenbank und Tracking-Konfiguration sichern. Bei einer bestehenden Tracking-Installation den Dienst für eine vollständige Sicherung von `tracking/state` kurz stoppen und alle Dateien dieses Verzeichnisses sichern. Bestehende SQLite-Daten und Importpositionen behalten.
2. `tracking/config.json` außerhalb des Git-Repositories anlegen. Bei einer neuen Verbindung die bisherige Floppy-Importposition `episode_actions_since` als `baseline` übernehmen oder einen Startzeitpunkt bewusst festlegen. `baseline` ist ein positiver Unix-Zeitstempel in Sekunden; niemals ungeprüft `0` setzen. Beispiel mit Platzhaltern:

   ```json
   {
     "upstream": "https://pinepods.example.com",
     "username": "mein-konto",
     "baseline": 1700000000,
     "exclude_devices": ["book-progress-sync", "book-progress-sync-test"],
     "aliases": [],
     "native_source": {
       "api_key_file": "/tracking/pinepods-api.key",
       "server_version": "0.9.0",
       "user_id": 2,
       "recovery_ids": []
     }
   }
   ```

   `user_id` ist die echte numerische ID des persönlichen PinePods-Kontos, nicht die ID des API-Schlüssels. Der Dienst prüft Konto und Schlüsselbesitz über die native Schnittstelle. `upstream` und `username` gelten für beide Schnittstellen desselben Kontos.
3. Den bereits vorhandenen **persönlichen PinePods-API-Schlüssel** als Text in `tracking/pinepods-api.key` hinterlegen. Kein neuer Schlüssel oder zusätzliches Passwort ist nötig. Datei und `config.json` erhalten Modus `0600` und müssen für UID 1000 lesbar sein; `tracking` und `tracking/state` gehören UID 1000 und sollten Modus `0700` haben. Schlüssel, private Feed-Adressen und Konfiguration bleiben außerhalb von GitHub und Protokollen.
4. `compose.tracking.yaml` als zusätzlichen Dockge-Stack verwenden. Den dort auskommentierten schreibgeschützten Bind-Mount für `pinepods-api.key` aktivieren, wenn `native_source` gesetzt ist. Die Quelldatei muss vorher existieren; `create_host_path: false` verhindert einen versehentlich angelegten Ordner. Für den alten Aktionsmodus bleibt dieser Mount auskommentiert. Das externe Netzwerk muss Floppys Netzwerk sein. Keinen Host-Port veröffentlichen.
5. In Floppys originalen GPodder-Einstellungen die Serveradresse `http://books-audio-tracking:8791` verwenden. Die vorhandenen PinePods-GPodder-Zugangsdaten und die Gerätekennung beibehalten. Ein unverändertes originales Floppy benötigt keine Quellcodeänderung und keinen Importer-Bind-Mount.
6. Die erste vollständige native Bestandsaufnahme durchführen und den Prüfstatus kontrollieren. Sie legt eine Vergleichsbasis an und erzeugt mit `recovery_ids: []` **keinen Hörvorgang aus der bisherigen Bibliothek**. Auch 16.000 bereits gehörte Folgen werden dadurch nicht als neue Hörvorgänge importiert. Anschließend eine bewusst geänderte Folge prüfen, erneut importieren und den Import nach einem Tracking-Neustart wiederholen. In Floppy die tatsächlich gespeicherten Folgen, Hörstände und vorhandenen Bewertungen vergleichen.

Der native Modus liest den vollständigen persönlichen Folgenbestand und die zur Zeitgrenze passenden nativen Aktivitätsdaten über die API. Er lädt keine Audiodateien und schreibt keine Hörstände nach PinePods. Eine unvollständige oder ungültige Quellprüfung liefert keine teilweise erfolgreiche Bestandsaufnahme. Fehlende Folgen gelten nicht als Zurücksetzen.

## Bestehenden Aktionsmodus umstellen

Bei einer bestehenden Installation `upstream`, `username`, `baseline`, `exclude_devices`, bestätigte `aliases` und das komplette SQLite-Verzeichnis unverändert übernehmen. Nur den geprüften `native_source`-Block und den Schlüssel-Mount ergänzen. Die Baseline ist an Quelle und Konto gebunden und wird durch Updates nicht neu erzeugt. Die native Erstaufnahme setzt die alte Historie nicht erneut in Bewegung.

Bereits fehlende Hörvorgänge müssen **vor der ersten nativen Aufnahme einzeln geprüft** werden: aktueller PinePods-Stand, native Aktivitätszeit, genaue RSS-Folgenidentität und vorhandene Floppy-Historie. Ausschließlich bestätigte, noch fehlende Folgen dürfen in `recovery_ids` stehen. Die ID hat die Form `"PinePods-User-ID:Podcast-ID:Episode-ID"`, beispielsweise `"2:10:20"`. Eine offene alte Antwort bleibt erhalten; dieselben bereits ausstehenden Ereignisse dürfen nicht zusätzlich als Wiederherstellung ausgewählt werden. Es gibt keinen Schalter zum pauschalen Wiederimport aller gehörten Folgen.

Diese Liste wird nur bei der ersten erfolgreichen nativen Vergleichsbasis ausgewertet und dort protokolliert. Wiederholte Aufrufe oder Neustarts erzeugen daraus keine weiteren Hörvorgänge. Ein späteres Ergänzen der Liste startet keine neue Migration. Nach einer möglichen Zustellung keine alte SQLite-Sicherung ungeprüft zurückspielen, da sie bereits bestätigte Ereignisse erneut anbieten könnte.

## Änderungen, Folgenidentität und Grenzen

Nach der Erstaufnahme werden tatsächliche Änderungen des nativen Abschlussstatus oder Hörfortschritts dauerhaft als Ereignisse gespeichert. Unveränderte Abschlussstände und reine Metadatenkorrekturen erzeugen keine weiteren Hörvorgänge. Ein später neu abonniertes altes Archiv erzeugt allein ebenfalls keinen Import aller früheren Abschlüsse. Für eine neu entdeckte alte Folge braucht es aktuelle native Aktivität; ein altes Veröffentlichungsdatum verhindert das heutige Hören nicht.

Im nativen Modus ist das GPodder-Aktionsprotokoll keine zweite Hörhistorienquelle. Empfangsbestätigungen, die Books & Audio selbst beim Hörstandabgleich schreibt, erzeugen deshalb keinen zusätzlichen Floppy-Hörvorgang. Ändert sich dabei der tatsächlich maßgebliche native PinePods-Stand, etwa nach dem Weiterhören in Audiobookshelf, bleibt diese Änderung für den Trackingdienst sichtbar. Die aus dem Aktionsmodus bekannte Liste `exclude_devices` unterdrückt solche legitimen nativen Änderungen nicht.

Vor der Übergabe an Floppy wird die genaue Audio-Adresse im aktuellen RSS-Feed geprüft. Feed-GUIDs und ausdrücklich bestätigte URL-Aliasse können Adresswechsel auflösen. Aliasse haben die Form `{"feed":"…","old":"…","new":"…","guid":"…"}` und gelten nur für den angegebenen Feed. Ähnliche Titel und Laufzeiten reichen nicht für eine Zuordnung. Nicht eindeutig auflösbare Ereignisse bleiben gespeichert und werden später erneut geprüft; eine leere Antwort darf sie nicht als erledigt markieren.

Floppy ist hier eine **Hörhistorie**, kein bidirektionaler Spiegel aller PinePods-Zustände. Ein ausdrückliches Zurücksetzen auf ungehört in PinePods löscht keinen früheren Floppy-Hörvorgang. Der Dienst merkt die neue Hörphase vor; erneutes Weiterhören oder Abschließen kann wieder als Ereignis erscheinen. Die Darstellung und Zusammenführung von Hörvorgängen übernimmt weiterhin Floppys originaler Importer.

## Quellstand und Importbestätigung prüfen

Der Dienst speichert Vergleichsbasis, ausstehende Antwort und bereits bestätigte Ereignisse in SQLite. Floppys nächste Anfrage mit der ausgegebenen Importposition bestätigt die vorherige Antwort. Bei Abbruch wird dieselbe gespeicherte Antwort erneut angeboten. Ein fehlgeschlagener Quellabruf setzt den bestätigten Importstand nicht zurück.

`GET /status` ist ein authentifizierter, lesender Endpunkt auf dem internen Trackingdienst. Er verwendet dieselben vorhandenen Basic-Zugangsdaten wie der GPodder-Import und startet selbst keinen Quellenabgleich. Er zeigt unter anderem `mode`, `baseline`, `acknowledged`, `pending_actions` sowie im nativen Modus `native_scan`, `baseline_episodes`, `projected_events`, `outstanding_events` und `blocked`.

`native_scan.at` und `native_scan.source` beschreiben die zuletzt erfolgreich gespeicherte native Quellprüfung; ihr Zeitpunkt muss mitgeprüft werden. `acknowledged` bestätigt den Transportcursor des Verbrauchers und ist **kein erneutes Auslesen der Floppy-Datenbank**. Offene beziehungsweise blockierte Ereignisse bleiben separat sichtbar. Ein erfolgreiches `/health` bestätigt nur, dass der Dienst antwortet. Die tatsächliche Übernahme in Floppy muss zusätzlich in dessen Oberfläche oder durch einen lesenden Datenvergleich geprüft werden.

## Zustellung einer einzelnen Folge

Ab Beta 17 lässt sich die Zustellung für eine einzelne Folge in der Podcastansicht
von Books & Audio nachschlagen. Dafür braucht der Webdienst lesenden Zugriff auf
das Zustandsverzeichnis des nativen Trackingdienstes. Die Titelsuche greift
auch auf PinePods-Folgen zu, die Audiobookshelf nicht verwaltet. Bereits
vorhandene Titel ohne gespeicherten Namen sind über ihre native Folgenkennung
auffindbar. Die nächste reguläre Bestandsaufnahme ergänzt die verfügbaren Namen.

Die Ansicht zeigt den zuletzt beobachteten Stand in PinePods, dessen verfügbare
Aktivitätszeit und das daraus erzeugte gPodder-Ereignis. Für ein angebotenes
Paket werden die konkrete Importposition und der Zeitpunkt der Bereitstellung
gespeichert. Die Bestätigung erfolgt, wenn der Verbraucher genau diese
Importposition zurücksendet. Bei früheren Betas ist für bereits bestätigte
Ereignisse häufig nur die Bestätigung selbst belegt. Die Speicherung in Floppy
erfordert einen zusätzlichen Beleg aus Floppy. Suchen und Öffnen der Spur lesen
die Datenbank auf dem Zustandsvolume und lösen keinen erneuten Import aus.

## Unabhängige Updates über Dockge

`compose.tracking.yaml` verwendet `ghcr.io/christophzepp/books-and-audio:tracking-beta`. Optional lässt sich der Wert mit `BOOKS_AUDIO_TRACKING_IMAGE` auf einen festen Versions-Tag setzen. Der Hauptstack verwendet weiterhin seinen Tag `beta`. Beide Tags verweisen bei Veröffentlichung auf dasselbe geprüfte Image für AMD64 und ARM64, lassen sich lokal aber unabhängig ziehen.

Zum Aktualisieren nur den Tracking-Stack in Dockge auswählen, Image ziehen und den Dienst neu erstellen. `tracking/config.json`, Schlüsseldatei und das bestehende Verzeichnis `tracking/state` behalten. Das Ziehen von `tracking-beta` verändert die lokal vorhandene Image-Zuordnung von `beta` nicht. Für die Tracking-Erweiterung von Beta7 kann der Hauptdienst auf Beta6 weiterlaufen; er muss dafür nicht neu erstellt werden. Ein Update von PinePods selbst über die geprüfte Version hinaus erfordert eine neue Schnittstellenprüfung.

## Prüfung vor einem größeren Rollout

Mit einer Kopie der Datenbank prüfen: Abschluss, Wiederaufnahme, ausdrückliches Zurücksetzen, Konflikt mit der primären Quelle, unterschiedliche Audiofassungen, alte Audio-URLs, wiederholter Import, Dienstneustart und vorübergehender Ausfall. Metadatenkorrekturen müssen vorhandene Folgen- und Hörvorgangs-IDs, Hördaten und Bewertungen erhalten. Erst danach die produktive Verbindung umstellen. Private Feed-URLs und Zugangsdaten gehören nicht in GitHub oder Fehlerprotokolle.

## Automatische Wiederaufnahme

Ein bereits per Folgenkennung bestätigter Feed-Wechsel wird auch für neue Folgen desselben Audiobookshelf-Podcasts verwendet, wenn PinePods die genaue Audio-Adresse im bestätigten persönlichen Abonnement führt. Beide gleichzeitig vorhandenen Feed-Ausgaben bleiben getrennt. Hörereignisse unter der früheren Adresse, eine andere Zuordnung oder eine mehrdeutige persönliche Bibliothek erfordern weiterhin Prüfung. Die Quellkonfiguration und die Metadaten der Original-Apps werden dabei nicht geändert.

Gültige Laufzeitmessungen bleiben bei späteren Netzfehlern oder verschobenen Prüfungen erhalten. Sie ersetzen keine frische Bestätigung vor einer Statusübertragung. Für vollständige Audioprüfungen gilt ein Abstand von 15 Minuten statt einer pauschalen Tagespause; je Zyklus bleiben Downloadanzahl, Größe und Laufzeit begrenzt. Frühere Tagespausen werden beim Update entsprechend verkürzt. Unterschiedliche bestätigte Fassungen verwenden weiterhin ausschließlich den Statusabgleich.

### Aktionsabruf durch den Podcast-Abgleich

Der Sync-Worker speichert seine gPodder-Leseposition und die vollständige Aktionshistorie der tatsächlich verwendeten Feeds in der Podcast-Zustandsdatenbank auf der NVMe. Die erste Beobachtung eines Feeds liest seine Historie einmal vollständig. In den folgenden Durchläufen genügt ein gemeinsamer Änderungsabruf für das verifizierte Konto. Eine Sekunde Überlappung erfasst gleichzeitig eingegangene Aktionen. Der Worker speichert Änderungen und Leseposition in derselben Datenbanktransaktion. Nach einem Neustart stehen damit auch die Änderungen für Folgen bereit, die beim Abbruch noch auf ihre Verarbeitung warteten.

PinePods 0.9.0 filtert die Aktionen nach der Ereigniszeit des Clients. Eine spät hochgeladene Offline-Aktion kann vor der zuletzt gelesenen Sekunde liegen. Innerhalb der eingestellten Podcast-Wartungszeit überprüft der Worker deshalb je Durchlauf einen bereits bekannten Feed vollständig über die API. Ältere Prüfungen haben Vorrang. Bei einer zurückgesetzten Serverzeit oder einem abgeschnittenen Änderungspaket baut der Worker die betroffenen Feed-Nachweise aus vollständigen Antworten wieder auf. Vor einer Übertragung liest er die beteiligten Dienste weiterhin direkt. Floppy verwaltet seine Abrufe und Bestätigungen unabhängig davon.

Zeigt die frische Prüfung vor einer Übertragung eine abweichende gPodder-Quittung, verwirft der Worker die betroffene Feed-Beobachtung. Der nächste Versuch lädt diesen Feed neu, auch nach einem Neustart. Eine Einzelantwort mit 25.000 Ereignissen kann in PinePods 0.9.0 abgeschnitten sein. Ein so großer kontoweiter Änderungsabruf löst die Prüfung je Feed aus. Erreicht ein einzelner Feed selbst diese Grenze, bleibt seine Übertragung gesperrt.

### Floppy-Lesebeleg

Die Weboberfläche liest die Zustellhistorie aus `/tracking-readonly/relay.sqlite`. Binde dazu im Webdienst das Zustandsverzeichnis des Tracking-Stacks unter `/tracking-readonly:ro` ein. Der Dienst vergleicht die Adresse und den Benutzernamen des aktiven PinePods-Profils mit dem Tracking-Konto. Nutzen beide Dienste verschiedene Adressen derselben Instanz, setze im Webdienst `TRACKING_PROFILE_ORIGIN` auf die geprüfte Adresse des Hauptdienstes und `TRACKING_RELAY_UPSTREAM` auf die entsprechende Adresse des Relays. Mit `TRACKING_PINE_USER_ID` lässt sich das native Konto zusätzlich festlegen.

Für die unabhängige Bestätigung in der Floppy-Datenbank:

1. Lege auf der NVMe ein privates Verzeichnis für die Ergebnisdatei an. Binde es im Webdienst als `/tracking-proof:ro` ein. Die UID des Webdienstes benötigt Leserechte.
2. Setze im Webdienst `FLOPPY_EVIDENCE_USER` auf den Floppy-Benutzernamen und `FLOPPY_EVIDENCE_RELAY_URL` auf die im Floppy-Konto gespeicherte Relay-Adresse. Übernimm diese beiden Werte in den folgenden Aufruf.
3. Führe das Hilfsprogramm auf dem Docker-Host mit der UID des Webdienstes aus. Verwende dazu die Datei `floppy_snapshot_host.py` aus dem GitHub-Release. Sie liest den Exporter aus dem laufenden Books-&-Audio-Webcontainer. Im kurzlebigen Floppy-Prozess bleiben Wartungsjobs aus. Eine dort konfigurierte SQLite-Transaktion vom Typ `IMMEDIATE` beginnt für den Leseexport als `DEFERRED`. Der Exporter liest den Floppy-Stand in einer schreibgeschützten Transaktion. Der Helfer ersetzt die Ergebnisdatei erst nach vollständiger Prüfung.

```sh
python3 floppy_snapshot_host.py --web-container WEB --floppy-container FLOPPY \
  --upstream https://pinepods.example --pine-user PINEPODS_BENUTZER \
  --floppy-user FLOPPY_BENUTZER --relay-url http://books-audio-tracking:8791 \
  --output /pfad/auf/nvme/floppy-proof/floppy.json
```

Setze für laufende Nachweise einen Host-Zeitplan, beispielsweise alle 30 Minuten. Nach drei Stunden gelten Belege als veraltet. Die Ergebnisdatei enthält gehashte Episodenschlüssel, Zustände und Zeitpunkte. Der Export schreibt keinen Hörverlauf und löst keinen Import aus. Ohne aktuellen, eindeutigen Treffer zeigt die GUI die nachweisbaren Transportschritte. Eine früher quittierte Lieferung kann auch nach dem Update ohne genaue Abrufzeit erscheinen.
