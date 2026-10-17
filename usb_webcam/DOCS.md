# USB Webcam

Macht eine am Home-Assistant-Gerät angeschlossene USB-Webcam (bis 4K, 30/60 fps, mit Ton)
zu einer ganz normalen `camera.*`-Entität. Im Dashboard läuft das Bild per WebRTC
mit voller Bildrate und Ton – genau wie bei einer IP-Kamera.

## So funktioniert es

1. Die App sucht die Kamera (`/dev/video*`) und liest alle Formate, Auflösungen und Bildraten aus.
2. Sie wählt automatisch den besten Weg:
   - **copy** – Kamera liefert selbst H.264 → wird nur durchgereicht (praktisch keine CPU-Last, ideal für 4K)
   - **vaapi** – Intel-/AMD-Grafik kodiert (Mini-PC, NUC)
   - **v4l2m2m** – Hardware-Encoder des Raspberry Pi 4 (maximal 1080p)
   - **software** – CPU kodiert (bei 4K nur auf schnellen x86-Rechnern sinnvoll)
3. Das Mikrofon der Kamera wird als zwei Tonspuren übertragen: AAC (für HLS) und Opus (für WebRTC).
4. [go2rtc](https://github.com/AlexxIT/go2rtc) stellt daraus einen RTSP-Stream bereit.
5. Beim ersten Start legt die App die Kamera automatisch über die Integration
   **Generic Camera** an und benennt sie (Standard: `camera.usb_webcam`).

## Einrichtung

1. Kamera anstecken, dann die App installieren und starten.
2. Optional: Tab **Konfiguration → Audio** – als Eingang das Mikrofon der Webcam wählen
   (die App findet USB-Mikrofone aber auch selbst).
3. Log der App ansehen. Dort steht, welche Kamera, welcher Modus und welcher Encoder gewählt wurde.
4. Unter **Einstellungen → Geräte & Dienste** erscheint die Integration *Generic Camera* mit der Webcam.
5. Ins Dashboard z. B. eine *Bildansicht*- oder *Kamera*-Karte mit `camera.usb_webcam` und
   Kameraansicht **Live** einfügen.

> Kamera erst nach dem App-Start eingesteckt oder umgesteckt? App neu starten.

## Wichtiger Hinweis zu 4K und 60 fps

- Die meisten 4K-Webcams liefern 4K nur als **MJPEG** mit 30 fps; 60 fps oft nur in 1080p.
  Was deine Kamera kann, steht im Log (`log_level: debug` zeigt alle Modi).
- **Raspberry Pi 4:** 4K kann nur ohne Umkodieren gestreamt werden, also wenn die Kamera selbst
  **H.264** liefert (`copy`). Bei MJPEG-Kameras auf dem Pi `resolution: 1080p` setzen – dann
  kodiert der Hardware-Encoder flüssig. Software-4K schafft der Pi nicht (die App warnt im Log).
- **Intel-/AMD-Mini-PC:** 4K30 per VAAPI ist kein Problem.

## Optionen

| Option | Bedeutung |
|---|---|
| `video_device` | `auto` oder z. B. `/dev/video0` |
| `resolution` | `auto` (höchste) oder `3840x2160`, `1920x1080`, `4k`, `1080p` |
| `fps` | Gewünschte Bildrate, z. B. `30` oder `60` |
| `input_format` | `auto`, `h264`, `mjpeg`, `nv12`, `yuyv422` |
| `encoder` | `auto`, `copy`, `vaapi`, `v4l2m2m`, `software` |
| `output_resolution` | `source` oder kleiner, z. B. `1080p` (spart Rechenleistung) |
| `bitrate_mbps` | `0` = automatisch |
| `audio` / `audio_source` | Ton an/aus, Quelle leer = automatisch |
| `camera_name` | Name der Entität in Home Assistant |
| `stream_name` | Name des Streams (Teil der RTSP-Adresse) |
| `auto_register` | Kamera automatisch in HA anlegen |
| `always_on` | Stream dauerhaft laufen lassen (schnellerer Start, aber Dauerlast) |
| `v4l2_controls` | Kamera-Einstellungen, Standard `power_line_frequency=1` (50 Hz gegen Flimmern) |
| `rtsp_username` / `rtsp_password` | Schutz, falls Port 8554 freigegeben wird |
| `custom_ffmpeg_command` | Nur für Experten, ersetzt die Automatik (muss `-f rtsp {output}` enthalten) |

## Zugriff von außerhalb von Home Assistant

Unter **Netzwerk** Port `8554` freigeben, dann ist der Stream z. B. in VLC unter
`rtsp://<IP-von-Home-Assistant>:8554/webcam` erreichbar. Dann am besten
`rtsp_username`/`rtsp_password` setzen. Die go2rtc-Oberfläche öffnet sich über
**Web-UI öffnen** bzw. in der Seitenleiste.

## Kamera manuell anlegen (falls die Automatik nicht klappt)

**Einstellungen → Geräte & Dienste → Integration hinzufügen → Generic Camera**, dann die beiden
Adressen aus dem Log der App eintragen:

- Standbild-URL: `http://<hostname>:1984/api/frame.jpeg?src=webcam`
- Stream-Quelle: `rtsp://<hostname>:8554/webcam?video&audio=all`
- Erweitert: RTSP-Transport `TCP`, SSL-Prüfung aus

## Fehlersuche

- **"Keine /dev/video* Geräte gefunden"** → Kamera ab- und anstecken, App neu starten.
  Unter **Einstellungen → System → Hardware** prüfen, ob `/dev/video0` existiert.
- **Kein Ton** → Tab *Konfiguration → Audio*: Kamera-Mikrofon als Eingang wählen, App neu starten.
- **Ruckeln** → Im Log nach "CPU schafft" suchen; `output_resolution: 1080p` oder `fps: 30` setzen.
- **Flimmern** → `power_line_frequency=1` (50 Hz) ist gesetzt; bei Bedarf `=2` (60 Hz) probieren.
- Die automatisch angelegte Kamera wurde gelöscht? Sie wird beim nächsten Start neu angelegt –
  `auto_register` ausschalten, wenn das nicht gewünscht ist.
