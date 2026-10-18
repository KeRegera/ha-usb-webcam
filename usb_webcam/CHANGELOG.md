# Changelog

## 2.0.0 – Neuaufbau

- Komplett neu und deutlich einfacher, nach dem Prinzip von MotionEye: Das MJPEG-Bild der Kamera
  wird direkt weitergereicht (volle 4K auch auf dem Raspberry Pi)
- Kamera wird im Konfigurations-Tab aus einer Liste gewählt (Pflichtfeld, immer sichtbar)
- Nur noch sechs Einstellungen: Kamera, Auflösung, Bilder/s, Datum/Uhrzeit an/aus, Position, Name
- Keine eigene Oberfläche mehr, kein Ton
- Kamera läuft dauerhaft – sofort Bild, Standbilder immer aktuell
- Prüft beim Start, ob die Kamera von einem anderen Programm (z. B. MotionEye) belegt ist
- Kamera-Einträge früherer Versionen werden in Home Assistant automatisch ersetzt

## 1.2.2

- Neu: Warnung im Log, wenn die Kamera schon von einem anderen Programm (z. B. MotionEye) belegt ist

## 1.2.1

- Fix: App wurde im App-Store nicht mehr angezeigt (ungültiger Gerätefilter in der Konfiguration)
- Wird in der Kamera-Auswahl ein Gerät gewählt, das keine Kamera ist, nimmt die App automatisch die Webcam

## 1.2.0

- Neu: MJPEG-Modus wie bei MotionEye – das Bild der Kamera wird direkt weitergereicht. Volle 4K auch auf dem Raspberry Pi 4, in Home Assistant als "MJPEG IP Camera" (ohne Ton)
- Neu: Option `stream_mode` (auto/mjpeg/h264). `auto` nimmt H.264 nur, wenn das Gerät es in voller Auflösung flüssig schafft, sonst MJPEG
- Neu: Kamera in den Optionen aus einer Liste auswählen (`camera_device`); ersetzt das Freitextfeld `video_device`
- Zeitstempel im MJPEG-Modus: Die App misst die Leistung und passt die Bildrate automatisch an
- Messergebnisse werden gespeichert – schnellerer Start ab dem zweiten Mal
- Raspberry-Pi-H.264-Encoder sendet SPS/PPS jetzt an jedem Keyframe
- Beim Moduswechsel wird die Kamera in Home Assistant automatisch umgestellt (Entität behält ihren Namen)

## 1.1.0

- Neu: Datum und Uhrzeit werden ins Videobild eingeblendet (Optionen timestamp, timestamp_position, timestamp_format) – damit auch in Aufnahmen und Standbildern
- Neu: Schafft das Gerät die volle Auflösung nicht (z. B. 4K auf dem Raspberry Pi), wechselt die App automatisch auf 1080p statt zu ruckeln

## 1.0.2

- WebRTC-Fehler "Failed to resolve hostname" behoben: Kamera wird jetzt über die interne IP der App statt über den Hostnamen angebunden
- Bestehende Kamera wird beim Start automatisch auf die neue Adresse umgestellt

## 1.0.1

- Build-Fehler behoben (Exit-Code 141 im Dockerfile)

## 1.0.0

- Erste Version
- Automatische Erkennung von USB-Kamera, Formaten, Auflösungen und Bildraten
- H.264-Passthrough, VAAPI (Intel/AMD), Raspberry-Pi-Hardware-Encoder oder Software
- Ton vom Kamera-Mikrofon als AAC (HLS) + Opus (WebRTC)
- Automatisches Anlegen als Kamera-Entität (Generic Camera) inkl. Umbenennen
- go2rtc 1.9.14 mit Weboberfläche über Ingress
