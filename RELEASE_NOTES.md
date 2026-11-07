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
