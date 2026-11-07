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
