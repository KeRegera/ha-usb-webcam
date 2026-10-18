# USB Webcam

Macht eine am Home-Assistant-Gerät angeschlossene USB-Webcam (bis 4K) zu einer ganz normalen
`camera.*`-Entität – auf Wunsch mit Datum/Uhrzeit im Bild.

## Die zwei Modi

| | **MJPEG** (wie MotionEye) | **H.264** |
|---|---|---|
| Wie | Bild der Kamera wird direkt weitergereicht | Bild wird in H.264 umkodiert |
| 4K auf dem Raspberry Pi 4 | ✅ flüssig | ❌ nur bis 1080p |
| Ton | ❌ | ✅ |
| In Home Assistant | *MJPEG IP Camera* | *Generic Camera* (WebRTC) |
| Datenmenge im Netz | hoch | gering |

Mit `stream_mode: auto` (Standard) entscheidet die App selbst: H.264 nur, wenn das Gerät es in
voller Auflösung flüssig schafft (z. B. Intel-Mini-PC oder Kamera mit eigenem H.264), sonst MJPEG.
Auf dem **Raspberry Pi 4** mit einer 4K-MJPEG-Webcam ist das MJPEG – genau wie bei MotionEye.

Wechselst du den Modus, stellt die App die Kamera in Home Assistant automatisch um.
Die Entität (z. B. `camera.usb_webcam`) behält ihren Namen.

## Einrichtung

1. Kamera anstecken, dann die App installieren und starten.
2. Log der App ansehen (Tab **Protokoll**). Dort steht, welche Kamera, welcher Modus und
   welche Auflösung/Bildrate gewählt wurde.
3. Unter **Einstellungen → Geräte & Dienste** erscheint die Kamera (*MJPEG IP Camera* bzw.
   *Generic Camera*).
4. Im Dashboard z. B. eine *Bildansicht*-Karte mit `camera.usb_webcam` und
   Kameraansicht **Live** einfügen. Ohne „Live“ zeigt die Karte nur alle paar Sekunden ein Standbild.

> Kamera erst nach dem App-Start eingesteckt oder umgesteckt? App neu starten.

## Kamera auswählen

Standardmäßig nimmt die App die erste USB-Kamera. Bei mehreren Kameras im Tab **Konfiguration**
unten **„Nicht verwendete optionale Konfigurationsoptionen anzeigen“** einschalten und bei
**Kamera** (`camera_device`) die gewünschte aus der Liste wählen.

Pro Webcam gibt es meist zwei Einträge: `…-video-index0` (das Bild) und `…-video-index1`
(nur Metadaten). Nimm **index0** – wählst du versehentlich index1, korrigiert die App das selbst.

## Datum und Uhrzeit im Bild

Standardmäßig blendet die App oben links Datum und Uhrzeit ein (z. B. `17.10.2026 20:49:05`).
Weil der Zeitstempel direkt ins Bild geschrieben wird, steht er auch in **jeder Aufnahme**
und jedem Standbild. Es gilt die Zeitzone von Home Assistant.

- `timestamp_position`: `top_left`, `top_right`, `bottom_left`, `bottom_right`
- `timestamp_format`: z. B. `%a %d.%m.%Y %H:%M:%S` für den Wochentag dazu
- Ausschalten mit `timestamp: false`

Für die Einblendung muss jedes Bild neu berechnet werden. Im MJPEG-Modus misst die App beim
ersten Start, wie schnell dein Gerät das schafft, und passt die Bildrate an
(Raspberry Pi 4 in 4K: einige Bilder pro Sekunde). **Ohne Zeitstempel** läuft MJPEG mit der
vollen Bildrate der Kamera und praktisch ohne Last.

## Optionen

| Option | Bedeutung |
|---|---|
| `camera_device` | Kamera aus der Liste (leer = automatisch) |
| `stream_mode` | `auto`, `mjpeg` (wie MotionEye) oder `h264` (mit Ton) |
| `resolution` | `auto` (höchste) oder `3840x2160`, `1920x1080`, `4k`, `1080p` |
| `fps` | Gewünschte Bildrate, z. B. `30` oder `60` |
| `input_format` | nur H.264-Modus: `auto`, `h264`, `mjpeg`, `nv12`, `yuyv422` |
| `encoder` | nur H.264-Modus: `auto`, `copy`, `vaapi`, `v4l2m2m`, `software` |
| `output_resolution` | nur H.264-Modus: `source` oder kleiner, z. B. `1080p` |
| `bitrate_mbps` | nur H.264-Modus: `0` = automatisch |
| `audio` / `audio_source` | nur H.264-Modus: Ton an/aus, Quelle leer = automatisch |
| `camera_name` | Name der Entität in Home Assistant |
| `stream_name` | Name des Streams in go2rtc |
| `auto_register` | Kamera automatisch in HA anlegen |
| `always_on` | Stream dauerhaft laufen lassen (sofortiges Bild, aber Dauerlast) |
| `v4l2_controls` | Kamera-Einstellungen, Standard `power_line_frequency=1` (50 Hz gegen Flimmern) |
| `rtsp_username` / `rtsp_password` | Schutz, falls Port 8554 freigegeben wird |
| `timestamp` / `timestamp_position` / `timestamp_format` | Datum/Uhrzeit einblenden, Position, Format |
| `custom_ffmpeg_command` | Nur für Experten, ersetzt die Automatik (muss `-f rtsp {output}` enthalten) |

## Zugriff von außerhalb / go2rtc-Oberfläche

Die go2rtc-Oberfläche öffnet sich über **Web-UI öffnen**. Im MJPEG-Modus zeigt die Seite
„stream“ dort kein Bild (sie kann nur H.264) – nimm stattdessen **links → MJPEG**.

Unter **Netzwerk** lassen sich Port `1984` (MJPEG: `http://<IP>:1984/api/stream.mjpeg?src=webcam`)
und `8554` (RTSP) freigeben, z. B. für VLC. Dann am besten `rtsp_username`/`rtsp_password` setzen.

## Kamera manuell anlegen (falls die Automatik nicht klappt)

Die Adressen stehen im Log der App.

- **MJPEG-Modus:** Integration **MJPEG IP Camera**, MJPEG-URL
  `http://<IP>:1984/api/stream.mjpeg?src=webcam`, Standbild `http://<IP>:1984/api/frame.jpeg?src=webcam`,
  SSL-Prüfung aus.
- **H.264-Modus:** Integration **Generic Camera**, Stream-Quelle
  `rtsp://<IP>:8554/webcam?video&audio=all`, Standbild wie oben, RTSP-Transport `TCP`.

## Fehlersuche

- **"Keine /dev/video* Geräte gefunden"** → Kamera ab- und anstecken, App neu starten.
- **Nur alle paar Sekunden ein neues Bild** → Karte auf Kameraansicht **Live** stellen.
- **Kein Ton** → Ton gibt es nur mit `stream_mode: h264` (auf dem Pi dann max. 1080p).
- **Zu wenige Bilder pro Sekunde mit Zeitstempel** → `timestamp: false` oder `resolution: 1080p`.
- **Flimmern** → `power_line_frequency=1` (50 Hz) ist gesetzt; bei Bedarf `=2` (60 Hz) probieren.
- Neue Hardware? `benchmark.json` wird automatisch neu gemessen, wenn sich das Gerät ändert.
- Die automatisch angelegte Kamera wurde gelöscht? Sie wird beim nächsten Start neu angelegt –
  `auto_register` ausschalten, wenn das nicht gewünscht ist.
