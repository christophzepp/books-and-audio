## Primäre Podcastquelle

- In der GUI unter Podcasts lässt sich PinePods, Audiobookshelf oder Jellyfin als primäre Quelle auswählen.
- Die Auswahl wirkt bei widersprüchlichen Anfangsständen und gleichzeitig geänderten Hörständen, sowohl im Positionsabgleich als auch beim Statusabgleich verschiedener Audiofassungen.
- Eindeutiges Weiterhören in einer anderen App wird weiterhin übernommen. Fehlende Daten, geänderte Identitäten und nicht bestätigte Audiofassungen bleiben geschützt.
- Vorhandene Installationen behalten beim Update die bisherige Konfliktprüfung ohne bevorzugte Quelle. Einstellungen, Verbindungen, Freigaben und Fortschritte werden weiterverwendet.
- BookOrbit-Podcasts bleiben vorbereitet. Floppy, PinePods und Audiobookshelf werden durch dieses Release nicht verändert.

Docker: `ghcr.io/christophzepp/books-and-audio:0.1.0-beta.3` oder der fortlaufende Tag `beta`, für Linux AMD64 und ARM64. Die angehängte Compose-Datei ist für Dockge vorbereitet.
