# Changelog

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
