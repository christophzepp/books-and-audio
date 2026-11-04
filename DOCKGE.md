# Books & Audio mit Dockge installieren

[Compose-Datei zum Kopieren](https://raw.githubusercontent.com/christophzepp/books-and-audio/main/compose.dockge.yaml) · [Docker-Image bei GitHub](https://github.com/users/christophzepp/packages/container/package/books-and-audio)

1. Lege in Dockge einen neuen Stack an, beispielsweise `books-and-audio-beta`. Füge den Inhalt der verlinkten Datei in das Feld **compose.yaml** ein.
2. Trage in der **.env** des Stacks unter `BOOKS_AUDIO_HOST` die IP-Adresse oder den DNS-Namen ein, den du im Browser verwendest. Der Wert enthält weder `http://` noch einen Pfad. Wenn Port 8790 schon vergeben ist, setze zusätzlich `BOOKS_AUDIO_PORT` auf einen freien Port.

   ```dotenv
   BOOKS_AUDIO_HOST=medienserver.lan
   BOOKS_AUDIO_PORT=8790
   ```

3. Klicke auf **Deploy**. Docker lädt das passende Image für Linux auf AMD64 oder ARM64. Der Dienst **init** richtet beim ersten Start die Volumes ein und endet mit Exitcode 0. **web** und **worker** laufen weiter.
4. Öffne die Konsole des Dienstes **web** in Dockge und führe `cat /admin-state/web-auth/setup-token` aus. Öffne im Beispiel `http://medienserver.lan:8790` und lege mit dem einmaligen Code dein Administratorkonto an.

Die neue Instanz beginnt mit leerer Bibliothek und pausierter Synchronisation. Die Anbindung von echten Medienservern benötigt in dieser Beta eine zur Installation passende Basiskonfiguration und eine wiederkehrende Prüfung der Docker-Container. Hinweise dazu stehen unten und in [BETRIEB.md](BETRIEB.md).

## Aktualisieren

In Dockge beim bestehenden Stack **Update/Aktualisieren** wählen. Docker lädt den aktuellen Tag `ghcr.io/christophzepp/books-and-audio:beta` und ersetzt die Container. Konto, Schlüssel, Verbindungen und Fortschrittsdaten liegen in den fünf persistenten Volumes des Stacks. Verwende bei Updates denselben Stacknamen. Bei einem Update bleiben der vorhandene Schlüssel und die gewählte Sync-Pause erhalten.

Über die Kommandozeile entsprechen dem `docker compose pull` und anschließend `docker compose up -d`. Wenn du bei einer bestimmten Version bleiben möchtest, setze in Dockge `BOOKS_AUDIO_IMAGE=ghcr.io/christophzepp/books-and-audio:0.1.0-beta.2`. Jeder Container-Release enthält die dazugehörige Compose-Datei.

## Eigene Bibliotheken anbinden

Für einen produktiven **neuen** Stack erst die Basiskonfiguration nach [BETRIEB.md](BETRIEB.md) vorbereiten. Vor dem ersten Deploy lässt sie sich aus dem Stackordner einlesen. Speichere die Compose-Datei dort als `compose.yaml`. Der folgende Befehl fragt die Dienstzugänge im Terminal ab.

```sh
docker compose run --rm -v /absoluter/pfad/basis.json:/basis.json:ro init python dockge_init.py --config /basis.json
```

Danach den Stack in Dockge starten. Ergänze die erforderlichen Netzwerkverbindungen und lesbaren Medienpfade für den Worker. Die private Basiskonfiguration liegt beim Image-Stack im Volume `config` unter `current/`. Der Worker speichert den Zustand im Volume `state`. Für die Hostprüfung erwartet `watch_deployment.py --root /pfad/zum/pruefverzeichnis` dort `config/config.json` und ein beschreibbares Verzeichnis `state/`. Verknüpfe auf dem Docker-Host `config` mit dem Unterverzeichnis `current` des Konfigurationsvolumes und `state` mit dem Zustandsvolume. Die Mountpoints kannst du per `docker volume inspect` ermitteln. Der Prüfprozess muss mit denselben Dateirechten wie der Worker (UID 1000) arbeiten und die Docker-CLI ausführen können.

Für eine Sicherung den Stack stoppen und die fünf zugehörigen Volumes gemeinsam sichern. Konfiguration, Schlüssel und Zustandsdaten gehören zu derselben Installation.
