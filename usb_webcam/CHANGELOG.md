# Changelog

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
