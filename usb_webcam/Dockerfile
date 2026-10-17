# https://developers.home-assistant.io/docs/apps/configuration#app-dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

ARG GO2RTC_VERSION=1.9.14
ARG GO2RTC_SHA256_AMD64=32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6
ARG GO2RTC_SHA256_ARM64=359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50
ARG TARGETARCH

# Pflicht: ffmpeg (inkl. VAAPI / V4L2-M2M / libx264 / libopus), V4L2-Tools, Python
RUN apk add --no-cache ffmpeg v4l-utils python3 \
    && command -v v4l2-ctl \
    && ffmpeg -hide_banner -version > /dev/null

# Schrift für die Datum/Uhrzeit-Einblendung
RUN apk add --no-cache font-dejavu || apk add --no-cache ttf-dejavu || echo "Hinweis: keine DejaVu-Schrift"

# Optional: PulseAudio-Client (Mikrofon-Erkennung), WebSocket (Entität umbenennen),
# Intel/AMD-VAAPI-Treiber (nur x86_64). Fehlt eins, läuft die App trotzdem.
RUN for pkg in pulseaudio-utils alsa-plugins-pulse py3-websocket-client; do \
        apk add --no-cache "${pkg}" || echo "Hinweis: ${pkg} nicht verfügbar"; \
    done \
    && if [ "$(apk --print-arch)" = "x86_64" ]; then \
        for pkg in intel-media-driver libva-intel-driver mesa-va-gallium; do \
            apk add --no-cache "${pkg}" || echo "Hinweis: ${pkg} nicht verfügbar"; \
        done; \
    fi

# go2rtc (Streaming-Server: RTSP, WebRTC, MSE, Snapshots)
RUN arch="${TARGETARCH:-$(apk --print-arch)}" \
    && case "${arch}" in \
        amd64|x86_64) go_arch="amd64"; sha="${GO2RTC_SHA256_AMD64}" ;; \
        arm64|aarch64) go_arch="arm64"; sha="${GO2RTC_SHA256_ARM64}" ;; \
        *) echo "Nicht unterstützte Architektur: ${arch}" && exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${go_arch}" \
    && echo "${sha}  /usr/local/bin/go2rtc" | sha256sum -c - \
    && chmod a+x /usr/local/bin/go2rtc

COPY rootfs /
RUN chmod a+x /run.sh /usr/local/bin/usb_webcam.py

CMD [ "/run.sh" ]

LABEL \
    org.opencontainers.image.title="Home Assistant App: USB Webcam" \
    org.opencontainers.image.description="USB-Webcam (bis 4K, mit Ton) als Kamera-Entität in Home Assistant" \
    org.opencontainers.image.licenses="MIT"
