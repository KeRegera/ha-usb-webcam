# USB Webcam

Macht eine USB-Webcam am Home-Assistant-Gerät zu einer ganz normalen Kamera in Home Assistant –
nach dem gleichen Prinzip wie MotionEye. Volle 4K funktionieren auch auf dem Raspberry Pi.

## Einrichtung

1. Andere Programme, die dieselbe Kamera nutzen (z. B. **MotionEye**), stoppen.
   Eine USB-Kamera kann immer nur ein Programm gleichzeitig benutzen.
2. Tab **Konfiguration** → bei **Kamera** deine Webcam aus der Liste wählen:
   den Eintrag `/dev/v4l/by-id/usb-…-video-index0`. Speichern.
3. App starten. Die Kamera erscheint automatisch in Home Assistant als `camera.usb_webcam`
   (Integration *MJPEG IP Camera*).
4. Im Dashboard eine Karte mit der Kamera hinzufügen und als Kameraansicht **Live** wählen.

## Einstellungen

| Einstellung | Bedeutung |
|---|---|
| Kamera | Deine Webcam aus der Liste (`…-video-index0`) |
| Auflösung | z. B. 3840x2160. Kann die Kamera sie nicht, wird die nächstkleinere genommen |
| Bilder pro Sekunde | z. B. 30 |
| Datum und Uhrzeit einblenden | Steht dann auch in Aufnahmen und Standbildern |
| Position von Datum und Uhrzeit | oben/unten, links/rechts |
| Kameraname | Name in Home Assistant |

## Gut zu wissen

- **Ohne Datum/Uhrzeit** wird das Bild der Kamera direkt weitergereicht: volle Auflösung,
  volle Bildrate, praktisch keine Last.
- **Mit Datum/Uhrzeit** muss jedes Bild neu berechnet werden. Die App misst beim ersten Start,
  was dein Gerät schafft, und stellt die Bildrate passend ein (Raspberry Pi 4 in 4K: einige
  Bilder pro Sekunde). Für mehr Bilder pro Sekunde eine kleinere Auflösung wählen.
- Ton gibt es bei diesem Verfahren nicht (wie bei MotionEye).

## Fehlersuche

Alles Wichtige steht im Tab **Protokoll** der App.

- **„wird gerade von einem anderen Programm benutzt“** → MotionEye o. Ä. stoppen, App neu starten.
- **„Bitte … Kamera auswählen“** → im Tab Konfiguration die Webcam wählen.
- **Kamera nach dem Start eingesteckt?** → App neu starten.
- **Nur alle paar Sekunden ein neues Bild** → in der Dashboard-Karte Kameraansicht **Live** wählen.
