## Stabilerer Podcast-Abgleich und originales Floppy

- Optionaler privater Trackingdienst für PinePods → Floppy: dauerhafter Importstand, wiederholbare Antworten bei Abbruch und Erkennung bereits importierter Hörereignisse. Auch tägliche Vollanfragen des originalen Floppy-Importers starten damit nicht erneut bei der gesamten Historie.
- Neue Hörereignisse zählen unabhängig vom Veröffentlichungsdatum der Folge. Nullpositionen und eigene Sync-Geräte erzeugen keine neuen Hörvorgänge. Bestätigte genaue Audio-URL-Aliasse sind pro Feed möglich.
- Die Podcast-GUI trennt „Position + Status“, „Nur Status“ für verschiedene Fassungen, wartende Zuordnungen und Sperren. Vor dem Übernehmen von Einstellungen wird der betroffene Umfang geprüft und angezeigt.
- Eine reine Änderung der Primärquelle kann unveränderte bestehende Identitätssperren erhalten. Sie gibt diese Folgen nicht frei; neue Sperren, andere Bibliotheken und offene Übertragungen werden weiterhin abgewiesen.
- PinePods, Audiobookshelf und Floppy benötigen keine Codeänderungen. BookOrbit bleibt bis zur geprüften originalen Podcast-Schnittstelle vorbereitet.

Docker: `ghcr.io/christophzepp/books-and-audio:0.1.0-beta.4` oder `beta`, für Linux AMD64 und ARM64. Die normale Dockge-Compose-Datei bleibt kompatibel. Der optionale Trackingdienst hat eine eigene Compose-Vorlage und Anleitung in `TRACKING.md`; vorhandene Importstände werden ausdrücklich übernommen.
