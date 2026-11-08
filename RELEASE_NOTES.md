## 0.1.0-beta.10 – Weniger Podcast-Dateiprüfungen

- Bereits geprüfte lokale Podcast-Dateizuordnungen werden bei unveränderten API-, Konto- und Mountidentitäten im Arbeitsspeicher wiederverwendet. Die Gültigkeit endet spätestens nach 15 Minuten und wird durch Cachetreffer nicht verlängert.
- Der Cache umfasst automatisch erkannte Audiofassungen, bestätigte manuelle Zuordnungen und persönliche Feed-Routen. Er erzeugt keine neue Laufzeitmessung und verändert keine gespeicherten Freigaben oder Hörstände.
- Geänderte Quellmetadaten, Konten oder Mountgenerationen, ein Neustart, fehlende Identitätsnachweise und zurückspringende Uhren erzwingen eine erneute lokale Prüfung. Vor tatsächlichen Statusänderungen und beim Wiederaufnehmen offener Übertragungen wird der Cache umgangen.
- Zusätzliche RAM-Zähler unterscheiden direkte Podcast-Dateiprüfungen von Cachetreffern. Sie messen Programmoperationen, keine physischen Festplattenzugriffe.
- 1.431 Tests bestanden, einschließlich 100 unveränderter Beobachtungen ohne Pfadauflösung, Dateiattributabfrage oder Dateiöffnung, fester Ablaufzeit und Schutz vor Dateiaustausch während einer Prüfung.

Docker: `ghcr.io/christophzepp/books-and-audio:0.1.0-beta.10` oder `beta`, für Linux AMD64 und ARM64. Bestehende Verbindungen, Freigaben, Hörstände und Dockge-Mounts bleiben kompatibel. Der separate Floppy-Trackingdienst benötigt für diese Optimierung kein Update.

## 0.1.0-beta.9 – Weniger Zugriffe auf Medienplatten

- Unveränderte, bestätigte E-Book-Stände werden zuerst über die aktuellen API- und Mountidentitäten verglichen. Ohne Änderungen entfallen Dateiöffnung, vollständiges Hashing und Indexaufbau; offene Übertragungsjournale und echte Änderungen werden weiterhin geprüft.
- EPUB-Positionsindizes werden als geprüfte, versionsgebundene JSON-Daten unter `state/ebook-index-cache` wiederverwendet, auch nach Containerneustarts. Der Cache ist begrenzt und ersetzt keine aktuelle Prüfung der Originaldatei vor einer Übertragung.
- Medienordner teilen einen Prüfstand mit standardmäßig 15 Minuten Gültigkeit. GUI-Aktualisierungen führen keine eigenen Ordnerdurchläufe mehr aus. Mount- und relevante Konfigurationsänderungen entwerten den Prüfstand; tatsächliche E-Book-Dateioperationen und manuelle Medienprüfungen kontrollieren die betroffenen Pfade erneut.
- Neue EPUB-Dateien werden erst nach stabiler Metadatenbeobachtung vollständig gelesen. Gültige negative Podcast-Wartefristen werden vor lokalen Pfadprüfungen ausgewertet; positive Bereitschaft und fällige Messungen behalten ihre bisherigen Prüfungen.
- Unveränderte GUI-Snapshots werden nicht ständig neu geschrieben. Der Lebensnachweis bleibt höchstens 60 Sekunden alt; dauerhafte Übertragungsjournale und die 150-Sekunden-Grenze für Deploymentnachweise bleiben erhalten.
- Zusätzliche Integritätsprüfungen mit optionalem UTC-Zeitfenster und Speichergrenzen sind unter `io_policy` konfigurierbar. RAM-Zähler zeigen Inhaltslesungen, Indexaufbauten, Cachetreffer und übersprungene Prüfungen. [Einrichtung und Messung](BETRIEB.md#medienzugriffe-ab-beta-9).
- 1.413 Tests bestanden, darunter 100 unveränderte Abgleiche ohne zusätzliche Inhaltslesungen oder Indexaufbauten sowie Änderungen während Übertragungen, Mountwechsel, beschädigte Caches und umgeleitete Pfade.

Docker: `ghcr.io/christophzepp/books-and-audio:0.1.0-beta.9` oder `beta`, für Linux AMD64 und ARM64. Bestehende Verbindungen, Freigaben und Fortschritte bleiben erhalten. Ein separater Tracking-Stack muss für diese Optimierung nicht neu erstellt werden.

## 0.1.0-beta.8 – Statusfreigaben während des Weiterhörens

- Eine bestätigte Freigabe des PinePods-Status „angefangen“ bleibt vor der Planerstellung bei reiner PinePods-Sekundenbewegung und deren abgeleiteten Revisionen verwendbar. Die Statuswerte aller Teilnehmer und sämtliche übrigen Rohdaten müssen unverändert sein. Der bisherige Vergleich konnte eine Freigabe direkt nach dem Weiterhören erneut sperren.
- Die Zuordnung zu Medien und Konten sowie die bestätigte Laufzeitversion bleiben gebunden. Zurücksetzen, Abschließen und verschwundene Hörstände werden weiterhin erkannt. Die strikten Prüfungen bei bereits geplanten Übertragungen und deren Rücklesekontrolle bleiben erhalten.
- Bestehende Freigaben bleiben nur verwendbar, wenn ihr ursprünglicher Prüfnachweis noch zu den gespeicherten Ausgangsdaten passt. Es werden keine alten Fortschritte neu importiert und keine Sekunden zwischen verschiedenen Audiofassungen umgerechnet.

Docker: `ghcr.io/christophzepp/books-and-audio:0.1.0-beta.8` oder `beta`, für Linux AMD64 und ARM64. Das Update betrifft den Hauptdienst; der getrennte Trackingdienst kann auf Beta7 weiterlaufen. Bestehende Verbindungen, Freigaben und Fortschritte bleiben erhalten.

## 0.1.0-beta.7 – PinePods als zuverlässigere Quelle für Floppy

- Der optionale Trackingdienst kann den nativen, kontogebundenen PinePods-Abschlussstatus und Hörfortschritt lesen. Damit erreichen auch Änderungen aus der originalen PinePods-Oberfläche den Import, wenn PinePods dafür keinen GPodder-Aktionseintrag erzeugt. Die geprüfte native Schnittstelle ist PinePods 0.9.0.
- Die erste vollständige native Bestandsaufnahme erzeugt keine Hörvorgänge aus der bisherigen Bibliothek. Bestehende Installationen behalten ihren Importstand und können einzeln geprüfte, bisher fehlende Folgen einmalig über `recovery_ids` übernehmen. Es gibt keinen pauschalen Wiederimport der Historie.
- Spätere Änderungen werden dauerhaft gespeichert und bis zur Importbestätigung erneut angeboten. Ein neuer alter Katalog erzeugt allein keine Hörvorgänge. Bestätigte eigene Sync-Aktionen werden nicht als zweite Quelle gezählt; eine echte Änderung des nativen PinePods-Hörstands bleibt übertragbar.
- Vor der Übergabe prüft der Dienst die genaue RSS-Folgenidentität. Nicht eindeutig auflösbare Ereignisse bleiben zur späteren Prüfung erhalten, statt ungeprüfte Ersatz-Metadaten an Floppy zu schicken. Der authentifizierte, lesende Endpunkt `/status` trennt Quellprüfung, offene Ereignisse und Transportbestätigung; er ersetzt keine Kontrolle der gespeicherten Floppy-Historie.
- Originale PinePods-, Audiobookshelf- und Floppy-Versionen bleiben verwendbar. Floppy bleibt eine Hörhistorie: Das Zurücksetzen einer Folge in PinePods löscht keinen früheren Floppy-Hörvorgang.
- Der separate Docker-Tag `tracking-beta` und die zusätzliche Release-Datei `compose.tracking.yaml` erlauben Updates des Tracking-Stacks über Dockge. Die bisherigen Verbindungen, Freigaben und Fortschritte des Hauptdienstes bleiben kompatibel. Für diese Tracking-Änderung muss ein laufender Hauptdienst auf Beta6 nicht neu erstellt werden.

Docker: `ghcr.io/christophzepp/books-and-audio:0.1.0-beta.7`, für Linux AMD64 und ARM64. Laufende Updates: `beta` für den Hauptdienst und `tracking-beta` für den optionalen Trackingdienst. Native Prüfung und Migration müssen gemäß [TRACKING.md](TRACKING.md) ausdrücklich eingerichtet werden; bestehende Konfigurationen ohne `native_source` bleiben im bisherigen Aktionsmodus.

## 0.1.0-beta.6 – Zuverlässigere Prüfungen und Wiederaufnahme

- Bei reinen BookOrbit-Metadatenrevisionen wird nach Prüfung der Medienidentität auch die Jellyfin-Vergleichsbasis aktualisiert. Bereits veraltete Basen benötigen einen passenden historischen Revisionsnachweis. Hörstände bleiben erhalten; geänderte Medien und offene Übertragungen werden weiterhin geschützt.
- Unabhängige Container- und Ordnerprüfungen laufen parallel. Ein interner Lauf-Lock und atomare Veröffentlichung verhindern Kollisionen überlappender Prüfungen. Der ursprüngliche Prüfzeitpunkt und die Gültigkeitsdauer bleiben unverändert; abgelaufene Nachweise geben keinen Abgleich frei.
- Die GUI ermöglicht eine gezielte erneute Prüfung zuvor freigegebener Podcast-Statuszuordnungen nach einer Identitätsänderung. Vorschau und verbleibende Sperren werden vor einer möglichen Freigabe angezeigt.
- Bei fehlender Serververbindung erscheint eine verständliche Meldung mit Hinweisen auf Serveradresse und Netzwerkverbindung statt „Load failed“.

Docker: `ghcr.io/christophzepp/books-and-audio:0.1.0-beta.6` oder `beta`, für Linux AMD64 und ARM64. Bestehende Dockge-Stacks, Verbindungen, Freigaben und Fortschritte bleiben kompatibel.

## 0.1.0-beta.5 – Automatische Wiederaufnahme beim Podcast-Abgleich

- Bereits bestätigte Feed-Wechsel gelten auch für neue Folgen mit exakt passender Audio-Adresse im selben persönlichen PinePods-Abonnement. Original-Apps und Abonnements werden nicht verändert.
- Bestehende Hörereignisse unter früheren Adressen, unterschiedliche Ausgaben und doppelte Zuordnungen werden ausdrücklich berücksichtigt. Es gibt keine Zuordnung allein anhand des Titels.
- Gültige Laufzeitmessungen gehen durch spätere Wiederholungsversuche oder Wartezeiten nicht mehr verloren. Statusübertragungen benötigen weiterhin einen frischen Nachweis.
- Vollständige Audioprüfungen erhalten einen begrenzten Abstand von 15 Minuten statt einer pauschalen 24-Stunden-Sperre. Bereits gespeicherte Tagespausen werden automatisch korrigiert.
- 15 neue Regressionstests prüfen Feed-Wechsel, fremde Konten, alte Hörereignisse, doppelte Zuordnungen und erhaltene Messnachweise.

Docker: `ghcr.io/christophzepp/books-and-audio:0.1.0-beta.5`, für Linux AMD64 und ARM64. Bestehende Dockge-Stacks und gespeicherte Fortschritte bleiben kompatibel.
