# PinePods, Audiobookshelf und Floppy

PinePods ist die bevorzugte Hörplattform. In Books & Audio lässt sich die primäre Podcastquelle unter **Podcasts → Einstellungen** auswählen. Bei echten Konflikten gewinnt deren geänderter Stand. Ein neues Weiterhören in der Backup-App wird trotzdem übernommen. Fehlende Daten werden nicht als Zurücksetzen gewertet.

Audiobookshelf lädt die Dateien und stellt sie Jellyfin bereit. Bei derselben Audiofassung werden Sekunden und Abschlussstatus übertragen. Bei bestätigten unterschiedlichen Fassungen überträgt Books & Audio nur angefangen/abgeschlossen. Es skaliert keine Hörpositionen zwischen Werbe- oder Premiumfassungen. Folgenkennungen, bestätigte Adresswechsel und Audiofassungen bleiben getrennte Prüfungen. BookOrbit bleibt bis zu einer geprüften originalen Podcast-Schnittstelle inaktiv.

## Originales Floppy mit inkrementellem Import

Floppys ursprünglicher GPodder-Importer fordert in regelmäßigen Abständen erneut die gesamte Historie an. Der optionale Books-&-Audio-Trackingdienst begrenzt auch solche Anfragen auf den bereits übernommenen Importstand. PinePods bleibt die Quelle; Floppy wird nicht gepatcht. Floppy schreibt keine Wiedergabeaktionen über diesen Dienst zurück.

1. Vorhandene Floppy-Datenbank mit SQLite-Backup sichern. Den aktuellen `episode_actions_since`-Wert des GPodder-Kontos übernehmen. Bei einer neuen Installation einen ausdrücklich gewählten Startzeitpunkt verwenden; niemals ungeprüft `0` setzen.
2. `tracking/config.json` außerhalb des Git-Repositories anlegen. Beispiel (Platzhalter ersetzen):

   ```json
   {"upstream":"https://pinepods.example.com","username":"mein-konto","baseline":1700000000,"exclude_devices":["book-progress-sync","book-progress-sync-test"],"aliases":[]}
   ```

3. Das Verzeichnis `tracking/state` für UID 1000 anlegen. `compose.tracking.yaml` als zusätzlichen Dockge-Stack verwenden oder den Dienst in einen bestehenden Stack aufnehmen. Das externe Netzwerk muss Floppys Netzwerk sein. Keinen Host-Port veröffentlichen.
4. In Floppys originalen GPodder-Einstellungen nur die Serveradresse auf `http://books-audio-tracking:8791` ändern. Vorhandene PinePods-Zugangsdaten und Gerätekennung beibehalten. Lokale Importer-Bind-Mounts entfernen und den originalen Floppy-Container neu erstellen.
5. Import zweimal ausführen und nach einem Neustart erneut prüfen. Keine alte Historie darf erneut als gehört erscheinen.

Der Dienst speichert Importstand, noch nicht bestätigte Antwort und bereits bestätigte Ereignisse in SQLite. Floppys nächste Anfrage mit der ausgegebenen Importposition bestätigt die vorherige Antwort. Bei Abbruch wird dieselbe Antwort erneut bereitgestellt; ein fehlgeschlagener Abruf setzt den Importstand nicht zurück. Die Baseline ist an Konto und Quelle gebunden und wird durch Updates nicht neu erzeugt.

Ein altes Veröffentlichungsdatum ist kein Ausschluss: Ein heute erzeugtes Hörereignis einer alten Folge wird importiert. Nullpositionen und Aktionen der eigenen Sync-Geräte erzeugen keine neuen Floppy-Hörvorgänge. Bestätigte, genaue URL-Aliasse können pro Feed als `{"feed":"…","old":"…","new":"…","guid":"…"}` hinterlegt werden. Es gibt keine Zuordnung über ähnliche Titel oder Laufzeiten.

## Prüfung vor einem größeren Rollout

Mit einer Kopie der Datenbank prüfen: Abschluss, Wiederaufnahme, ausdrückliches Zurücksetzen, Konflikt mit der primären Quelle, unterschiedliche Audiofassungen, alte Audio-URLs, wiederholter Import, Dienstneustart und vorübergehender Ausfall. Metadatenkorrekturen müssen vorhandene Folgen- und Hörvorgangs-IDs, Hördaten und Bewertungen erhalten. Erst danach die produktive Verbindung umstellen. Private Feed-URLs und Zugangsdaten gehören nicht in GitHub oder Fehlerprotokolle.

## Automatische Wiederaufnahme

Ein bereits per Folgenkennung bestätigter Feed-Wechsel wird auch für neue Folgen desselben Audiobookshelf-Podcasts verwendet, wenn PinePods die genaue Audio-Adresse im bestätigten persönlichen Abonnement führt. Beide gleichzeitig vorhandenen Feed-Ausgaben bleiben getrennt. Hörereignisse unter der früheren Adresse, eine andere Zuordnung oder eine mehrdeutige persönliche Bibliothek erfordern weiterhin Prüfung. Die Quellkonfiguration und die Metadaten der Original-Apps werden dabei nicht geändert.

Gültige Laufzeitmessungen bleiben bei späteren Netzfehlern oder verschobenen Prüfungen erhalten. Sie ersetzen keine frische Bestätigung vor einer Statusübertragung. Für vollständige Audioprüfungen gilt ein Abstand von 15 Minuten statt einer pauschalen Tagespause; je Zyklus bleiben Downloadanzahl, Größe und Laufzeit begrenzt. Frühere Tagespausen werden beim Update entsprechend verkürzt. Unterschiedliche bestätigte Fassungen verwenden weiterhin ausschließlich den Statusabgleich.
