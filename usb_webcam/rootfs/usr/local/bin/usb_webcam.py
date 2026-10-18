#!/usr/bin/env python3
"""USB Webcam – Home Assistant OS App.

Ablauf beim Start:
  1. USB-Kamera (V4L2) finden und ihre Formate/Auflösungen/FPS auslesen
  2. Modus wählen:
     - MJPEG: Bild der Kamera direkt weiterreichen (wie MotionEye) – volle 4K auch
       auf dem Raspberry Pi, kein Ton. In HA als "MJPEG IP Camera".
     - H.264: Kodieren (Passthrough, VAAPI, Raspberry-Pi-HW, Software) mit Ton
       (AAC + Opus). In HA als "Generic Camera" mit WebRTC.
  3. go2rtc mit einem ffmpeg-Stream starten
  4. Kamera einmalig in Home Assistant anlegen -> normale camera.* Entität
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

OPTIONS_PATH = os.environ.get("USBCAM_OPTIONS", "/data/options.json")
STATE_PATH = os.environ.get("USBCAM_STATE", "/data/state.json")
GO2RTC_CONFIG = os.environ.get("USBCAM_GO2RTC_CONFIG", "/tmp/go2rtc.yaml")
GO2RTC_BIN = os.environ.get("USBCAM_GO2RTC_BIN", "go2rtc")
TIMESTAMP_FILE = os.environ.get("USBCAM_TIMESTAMP_FILE", "/tmp/usb_webcam_timestamp.txt")
BENCH_PATH = os.environ.get("USBCAM_BENCH", "/data/benchmark.json")
SYSFS_V4L = os.environ.get("USBCAM_SYSFS_V4L", "/sys/class/video4linux")
SUPERVISOR_URL = os.environ.get("USBCAM_SUPERVISOR_URL", "http://supervisor")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

API_PORT = 1984
RTSP_PORT = 8554
WEBRTC_PORT = 8555

DEFAULTS = {
    "camera_device": "",
    "stream_mode": "auto",
    "resolution": "auto",
    "fps": 30,
    "input_format": "auto",
    "encoder": "auto",
    "output_resolution": "source",
    "bitrate_mbps": 0,
    "audio": True,
    "audio_source": "",
    "stream_name": "webcam",
    "camera_name": "USB Webcam",
    "auto_register": True,
    "always_on": False,
    "v4l2_controls": ["power_line_frequency=1"],
    "rtsp_username": "",
    "rtsp_password": "",
    "custom_ffmpeg_command": "",
    "timestamp": True,
    "timestamp_position": "top_left",
    "timestamp_format": "%d.%m.%Y %H:%M:%S",
    "log_level": "info",
}

# V4L2-FourCC -> ffmpeg "-input_format"
FOURCC_TO_FFMPEG = {
    "H264": "h264",
    "MJPG": "mjpeg",
    "JPEG": "mjpeg",
    "NV12": "nv12",
    "YUYV": "yuyv422",
    "YU12": "yuv420p",
    "UYVY": "uyvy422",
}
INPUT_OPTION_TO_FOURCC = {
    "h264": ["H264"],
    "mjpeg": ["MJPG", "JPEG"],
    "nv12": ["NV12"],
    "yuyv422": ["YUYV"],
}
AUTO_ORDER = ["H264", "MJPG", "JPEG", "NV12", "YUYV", "YU12", "UYVY"]
RES_ALIASES = {
    "4k": (3840, 2160),
    "2160p": (3840, 2160),
    "1440p": (2560, 1440),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "480p": (640, 480),
}

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_log_level = 20


def log(level: str, msg: str) -> None:
    if _LEVELS.get(level, 20) >= _log_level:
        print(f"[usb_webcam] {level.upper():<7} {msg}", flush=True)


def run(cmd: list[str], timeout: float = 20, env: dict | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: nicht gefunden"
    except subprocess.TimeoutExpired:
        return 124, "Timeout"


# --------------------------------------------------------------------------- #
# Optionen
# --------------------------------------------------------------------------- #
def load_options() -> dict:
    opts = dict(DEFAULTS)
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as fh:
            opts.update({k: v for k, v in json.load(fh).items() if v is not None})
    except FileNotFoundError:
        log("warning", f"{OPTIONS_PATH} fehlt – nutze Standardwerte")
    # Bis 1.1.0 hieß die Option "video_device" (Freitext) – weiterhin auswerten
    legacy = str(opts.pop("video_device", "") or "").strip()
    if not opts.get("camera_device") and legacy and legacy != "auto":
        opts["camera_device"] = legacy
    opts["camera_device"] = str(opts.get("camera_device") or "").strip()
    opts["stream_name"] = re.sub(r"[^a-zA-Z0-9_-]", "_", str(opts["stream_name"])) or "webcam"
    opts["fps"] = int(opts["fps"])
    return opts


def parse_resolution(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    value = str(value).strip().lower()
    if value in RES_ALIASES:
        return RES_ALIASES[value]
    m = re.fullmatch(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", value)
    return (int(m.group(1)), int(m.group(2))) if m else None


def slugify(text: str) -> str:
    text = text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "usb_webcam"


# --------------------------------------------------------------------------- #
# V4L2-Geräte
# --------------------------------------------------------------------------- #
def parse_v4l2_info(text: str) -> dict:
    info = {"driver": "", "card": "", "bus": "", "caps": []}
    lines = text.splitlines()
    for line in lines:
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower()
            if key == "driver name":
                info["driver"] = val.strip()
            elif key == "card type":
                info["card"] = val.strip()
            elif key == "bus info":
                info["bus"] = val.strip()
    # Die "Device Caps" beschreiben genau diesen Knoten (nicht das ganze Gerät).
    for idx, line in enumerate(lines):
        if line.strip().lower().startswith("device caps"):
            base = len(line) - len(line.lstrip())
            for sub in lines[idx + 1 :]:
                if not sub.strip():
                    break
                if len(sub) - len(sub.lstrip()) <= base:
                    break
                info["caps"].append(sub.strip())
            break
    if not info["caps"]:  # ältere v4l2-ctl Ausgabe: nur "Capabilities"
        for idx, line in enumerate(lines):
            if line.strip().lower().startswith("capabilities"):
                base = len(line) - len(line.lstrip())
                for sub in lines[idx + 1 :]:
                    if not sub.strip() or len(sub) - len(sub.lstrip()) <= base:
                        break
                    info["caps"].append(sub.strip())
                break
    return info


def parse_v4l2_formats(text: str) -> dict[str, dict[tuple[int, int], list[float]]]:
    """Parst `v4l2-ctl --list-formats-ext` -> {FOURCC: {(w, h): [fps, ...]}}."""
    formats: dict[str, dict[tuple[int, int], list[float]]] = {}
    fourcc = None
    size = None
    for line in text.splitlines():
        m = re.search(r"\[\d+\]:\s*'([^']+)'", line)
        if m:
            fourcc = m.group(1).strip().upper()
            formats.setdefault(fourcc, {})
            size = None
            continue
        if fourcc is None:
            continue
        m = re.search(r"Size:\s*Discrete\s+(\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            formats[fourcc].setdefault(size, [])
            continue
        m = re.search(r"Size:\s*(?:Stepwise|Continuous)\s+\d+x\d+\s*-\s*(\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            formats[fourcc].setdefault(size, [])
            continue
        m = re.search(r"\(([\d.]+)(?:\s*-\s*([\d.]+))?\s*fps\)", line)
        if m and size is not None:
            fps = float(m.group(2) or m.group(1))
            if fps not in formats[fourcc][size]:
                formats[fourcc][size].append(fps)
    for sizes in formats.values():
        for key in sizes:
            sizes[key].sort()
    return formats


def is_capture_node(info: dict) -> bool:
    caps = [c.lower() for c in info.get("caps", [])]
    if any("memory-to-memory" in c for c in caps):
        return False
    return any(c in ("video capture", "video capture multiplanar") for c in caps)


def probe_device(path: str) -> dict:
    _, info_txt = run(["v4l2-ctl", "-d", path, "--info"])
    _, fmt_txt = run(["v4l2-ctl", "-d", path, "--list-formats-ext"])
    info = parse_v4l2_info(info_txt)
    info["path"] = path
    info["formats"] = parse_v4l2_formats(fmt_txt)
    return info


def max_pixels(formats: dict) -> int:
    return max((w * h for sizes in formats.values() for (w, h) in sizes), default=0)


def list_video_nodes() -> list[str]:
    def num(p: str) -> int:
        m = re.search(r"(\d+)$", p)
        return int(m.group(1)) if m else 9999

    return sorted(glob.glob("/dev/video*"), key=num)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def sysfs_nodes() -> dict[str, dict]:
    """/dev/videoN -> {index, usb (USB-Geräteordner), iface (Interface-Ordner), serial, product}."""
    nodes = {}
    for entry in glob.glob(os.path.join(SYSFS_V4L, "video*")):
        name = os.path.basename(entry)
        iface = os.path.realpath(os.path.join(entry, "device"))
        usb = os.path.dirname(iface)
        nodes["/dev/" + name] = {
            "index": _read(os.path.join(entry, "index")) or "0",
            "iface": os.path.basename(iface),
            "usb": usb,
            "serial": _read(os.path.join(usb, "serial")),
            "product": _read(os.path.join(usb, "product")),
            "manufacturer": _read(os.path.join(usb, "manufacturer")),
        }
    return nodes


def resolve_device_path(option: str) -> str | None:
    """Wandelt die Auswahl aus den App-Optionen in /dev/videoN um.

    Die Auswahlliste von Home Assistant liefert Pfade wie
    /dev/v4l/by-id/usb-<Hersteller>_<Modell>_<Seriennr>-video-index0, die es im
    Container nicht immer gibt – dann über /sys auflösen.
    """
    if os.path.exists(option):
        return os.path.realpath(option)
    base = os.path.basename(option)
    nodes = sysfs_nodes()
    m = re.search(r"-video-index(\d+)$", base)
    index = m.group(1) if m else None

    m_path = re.search(r"-usb(?:v\d)?-\d+:([\d.]+):(\d+\.\d+)-video-index\d+$", base)
    for dev, info in sorted(nodes.items()):
        if index is not None and info["index"] != index:
            continue
        if m_path and info["iface"].endswith(f"-{m_path.group(1)}:{m_path.group(2)}"):
            return dev
        if "/by-id/" in option or base.startswith("usb-"):
            serial = info["serial"]
            if serial and f"_{serial}-video-index" in base:
                return dev
            label = re.sub(r"\s+", "_", f"{info['manufacturer']}_{info['product']}")
            if info["product"] and label and label in base:
                return dev
    return None


def capture_sibling(dev: dict, probes: list[dict]) -> dict | None:
    """Gleiches USB-Gerät, aber der Knoten mit dem Videobild (index0 statt Metadaten)."""
    nodes = sysfs_nodes()
    usb = nodes.get(dev["path"], {}).get("usb")
    if not usb:
        return None
    for other in probes:
        if other["path"] != dev["path"] and nodes.get(other["path"], {}).get("usb") == usb \
                and is_capture_node(other) and other["formats"]:
            return other
    return None


def select_device(option: str, probes: list[dict]) -> dict | None:
    if option and option != "auto":
        path = resolve_device_path(option)
        if not path:
            log("error", f"Ausgewähltes Gerät {option} nicht gefunden. Ist die Kamera eingesteckt? "
                         "Nach dem Einstecken die App neu starten.")
            return None
        if path != option:
            log("info", f"Ausgewähltes Gerät {option} -> {path}")
        if not re.fullmatch(r"/dev/video\d+", path):
            log("warning", f"{option} ist keine Kamera – wähle automatisch. Tipp: in der Liste den "
                           "Eintrag '/dev/v4l/by-id/…-video-index0' nehmen")
            return select_device("", probes)
        dev = next((p for p in probes if os.path.realpath(p["path"]) == path), None) or probe_device(path)
        if not is_capture_node(dev) or not dev["formats"]:
            sib = capture_sibling(dev, probes)
            if sib:
                log("warning", f"{path} liefert kein Videobild (Metadaten-Knoten) – nehme {sib['path']} "
                               "derselben Kamera. Tipp: in der Auswahl den Eintrag mit 'index0' wählen.")
                return sib
            log("error", f"{path} liefert keine Videoformate (falsches Gerät? Eintrag mit 'index0' wählen)")
            return None
        return dev
    candidates = [p for p in probes if is_capture_node(p) and p["formats"]]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p["driver"] == "uvcvideo", max_pixels(p["formats"])), reverse=True)
    return candidates[0]


def best_fps(fps_list: list[float], wanted: int) -> float:
    if not fps_list:
        return float(wanted)  # Gerät meldet keine Intervalle -> anfordern, was gewünscht ist
    ok = [f for f in fps_list if f <= wanted + 0.5]
    return max(ok) if ok else min(fps_list)


def choose_mode(formats: dict, resolution_opt: str, fps_wanted: int, input_opt: str, encoder_opt: str) -> dict:
    usable = {f: s for f, s in formats.items() if f in FOURCC_TO_FFMPEG and s}
    if not usable:
        raise RuntimeError(f"Kein unterstütztes Videoformat gefunden: {list(formats)}")

    all_res = {r for sizes in usable.values() for r in sizes}
    wanted_res = parse_resolution(resolution_opt) if resolution_opt != "auto" else None
    if wanted_res:
        if wanted_res in all_res:
            target = wanted_res
        else:
            smaller = [r for r in all_res if r[0] * r[1] <= wanted_res[0] * wanted_res[1]]
            pool = smaller or list(all_res)
            target = max(pool, key=lambda r: r[0] * r[1]) if smaller else min(pool, key=lambda r: r[0] * r[1])
            log("warning", f"Auflösung {resolution_opt} nicht verfügbar – nehme {target[0]}x{target[1]}")
    else:
        compressed = {f: s for f, s in usable.items() if f in ("H264", "MJPG", "JPEG")} or usable
        target = max(
            {r for s in compressed.values() for r in s},
            key=lambda r: (r[0] * r[1], max((best_fps(s.get(r, []), fps_wanted) for s in compressed.values()), default=0)),
        )

    order = list(AUTO_ORDER)
    if encoder_opt not in ("auto", "copy"):
        # Wird ohnehin neu kodiert -> MJPEG ist billiger zu dekodieren als H.264
        order.remove("H264")
        order.insert(2, "H264")
    allowed = INPUT_OPTION_TO_FOURCC.get(input_opt) if input_opt != "auto" else None

    def candidates(allowed_list):
        result = []
        for fcc, sizes in usable.items():
            if allowed_list and fcc not in allowed_list:
                continue
            if target not in sizes:
                continue
            fps = best_fps(sizes[target], fps_wanted)
            rank = order.index(fcc) if fcc in order else len(order)
            result.append((round(min(fps, fps_wanted + 0.5)), -rank, fcc, fps))
        return result

    cands = candidates(allowed)
    if not cands and allowed:
        log("warning", f"Eingangsformat {input_opt} bei {target[0]}x{target[1]} nicht verfügbar – wähle automatisch")
        cands = candidates(None)
    if not cands:
        raise RuntimeError("Kein passender Kameramodus gefunden")
    _, _, fcc, fps = max(cands)
    if fps + 0.5 < fps_wanted:
        hint = sorted(
            {r for sizes in usable.values() for r, fl in sizes.items() if any(abs(x - fps_wanted) < 0.6 for x in fl)},
            key=lambda r: -r[0] * r[1],
        )
        extra = f" – {fps_wanted} fps gibt es bei: {', '.join(f'{w}x{h}' for w, h in hint[:4])}" if hint else ""
        log("warning", f"Kamera schafft bei {target[0]}x{target[1]} ({fcc}) nur {fps:g} fps statt {fps_wanted}{extra}")
    return {"fourcc": fcc, "width": target[0], "height": target[1], "fps": fps}


class FFmpegCaps:
    def __init__(self) -> None:
        _, enc = run(["ffmpeg", "-hide_banner", "-encoders"])
        _, dmx = run(["ffmpeg", "-hide_banner", "-demuxers"])
        self.encoders = set(re.findall(r"^\s*[VAS][\w.]{5}\s+(\S+)", enc, re.M))
        self.demuxers = set(re.findall(r"^\s*D\S*\s+(\S+)", dmx, re.M))

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def has_demuxer(self, name: str) -> bool:
        return name in self.demuxers


def render_node() -> str | None:
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else None


def test_encoder(enc: str, width: int, height: int, fps: float) -> tuple[bool, str]:
    src = ["-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={max(1, round(fps))}"]
    if enc == "vaapi":
        node = render_node()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-init_hw_device", f"vaapi=va:{node}",
               "-filter_hw_device", "va", *src, "-frames:v", "10", "-vf", "format=nv12,hwupload",
               "-c:v", "h264_vaapi", "-bf", "0", "-f", "null", "-"]
    elif enc == "v4l2m2m":
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *src, "-frames:v", "10",
               "-pix_fmt", "yuv420p", "-c:v", "h264_v4l2m2m", "-b:v", "4M", "-f", "null", "-"]
    else:
        return True, ""
    rc, out = run(cmd, timeout=40)
    return rc == 0, out.strip()[-300:]


def cached_measure(key: str, fn) -> float:
    """Messwerte in /data merken – die Messung kostet auf dem Pi sonst bei jedem Start Zeit."""
    key = f"{platform.machine()}-{os.cpu_count()}:{key}"
    try:
        with open(BENCH_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if key in data:
        return float(data[key])
    value = float(fn())
    if value > 0:
        data[key] = round(value, 1)
        try:
            with open(BENCH_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
        except OSError:
            pass
    return value


def benchmark_software(width: int, height: int, fps: float, bitrate: int) -> float:
    def measure() -> float:
        frames = max(15, int(round(fps * 1.5)))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               f"testsrc2=size={width}x{height}:rate={max(1, round(fps))}", "-frames:v", str(frames),
               "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
               "-b:v", f"{bitrate}M", "-f", "null", "-"]
        start = time.monotonic()
        rc, _ = run(cmd, timeout=120)
        elapsed = time.monotonic() - start
        return frames / elapsed if rc == 0 and elapsed > 0 else 0.0

    return cached_measure(f"x264:{width}x{height}@{fps:g}:{bitrate}", measure)


def benchmark_mjpeg_overlay(width: int, height: int, overlay: str) -> float:
    """Wie schnell kann das Gerät MJPEG dekodieren + Zeitstempel zeichnen + neu kodieren?"""
    def measure() -> float:
        sample = f"/tmp/usb_webcam_bench_{width}x{height}.mjpeg"
        rc, _ = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                     f"testsrc2=size={width}x{height}:rate=10", "-frames:v", "10", "-pix_fmt", "yuvj422p",
                     "-c:v", "mjpeg", "-q:v", "5", sample], timeout=120)
        if rc != 0:
            return 0.0
        frames = 30
        start = time.monotonic()
        rc, _ = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "2", "-i", sample,
                     "-vf", overlay, "-c:v", "mjpeg", "-q:v", "5", "-threads", "0", "-f", "null", "-"],
                    timeout=180)
        elapsed = time.monotonic() - start
        try:
            os.remove(sample)
        except OSError:
            pass
        return frames / elapsed if rc == 0 and elapsed > 0 else 0.0

    return cached_measure(f"mjpeg-ts:{width}x{height}", measure)


def choose_encoder(opt: str, mode: dict, out_w: int, out_h: int, caps: FFmpegCaps, bitrate: int,
                   overlay: bool = False) -> tuple[str, bool]:
    """Liefert (encoder, schnell_genug)."""
    same_size = (out_w, out_h) == (mode["width"], mode["height"])
    if mode["fourcc"] == "H264" and same_size and opt in ("auto", "copy") and not overlay:
        return "copy", True
    if opt == "copy":
        reason = "Zeitstempel-Einblendung aktiv" if overlay else "Kamera liefert kein H.264 oder Skalierung aktiv"
        log("warning", f"encoder=copy nicht möglich ({reason}) – wähle automatisch")
        opt = "auto"

    order = ["vaapi", "v4l2m2m", "software"] if opt == "auto" else [opt]
    for enc in order:
        if enc == "vaapi":
            if not render_node() or not caps.has_encoder("h264_vaapi"):
                if opt != "auto":
                    log("warning", "VAAPI nicht verfügbar (kein /dev/dri/renderD*)")
                continue
        if enc == "v4l2m2m" and not caps.has_encoder("h264_v4l2m2m"):
            continue
        if enc == "software":
            break
        ok, err = test_encoder(enc, out_w, out_h, mode["fps"])
        if ok:
            return enc, True
        log("info" if opt == "auto" else "warning",
            f"Hardware-Encoder {enc} bei {out_w}x{out_h} nicht nutzbar{': ' + err if err else ''}")

    speed = benchmark_software(out_w, out_h, mode["fps"], bitrate)
    if speed and speed < mode["fps"] * 0.95:
        log("warning",
            f"CPU schafft Software-Kodierung bei {out_w}x{out_h} nur mit ca. {speed:.1f} fps "
            f"(gewünscht {mode['fps']:g}).")
        return "software", False
    if speed:
        log("info", f"Software-Kodierung: ca. {speed:.0f} fps möglich bei {out_w}x{out_h}")
    return "software", True


def find_font() -> str | None:
    for pattern in ("/usr/share/fonts/**/DejaVuSans-Bold.ttf", "/usr/share/fonts/**/DejaVuSans.ttf",
                    "/usr/share/fonts/**/*.ttf"):
        hits = sorted(glob.glob(pattern, recursive=True))
        if hits:
            return hits[0]
    return None


def build_timestamp_filter(opts: dict, frame_h: int, textfile: str) -> str | None:
    """drawtext-Filter für Datum/Uhrzeit. Der Text kommt aus einer Datei, damit
    weder Leerzeichen noch Doppelpunkte im ffmpeg-/go2rtc-Befehl escaped werden müssen."""
    if not opts.get("timestamp"):
        return None
    fmt = str(opts.get("timestamp_format") or "%d.%m.%Y %H:%M:%S")
    # In der drawtext-Expansion trennt ":" Argumente -> im Format escapen
    fmt = fmt.replace("\\", "\\\\").replace(":", "\\:").replace("}", "")
    with open(textfile, "w", encoding="utf-8") as fh:
        fh.write("%{localtime:" + fmt + "}")
    size = max(14, round(frame_h / 30))
    pad = max(8, round(frame_h / 60))
    pos = {
        "top_left": (f"{pad}", f"{pad}"),
        "top_right": (f"w-tw-{pad}", f"{pad}"),
        "bottom_left": (f"{pad}", f"h-th-{pad}"),
        "bottom_right": (f"w-tw-{pad}", f"h-th-{pad}"),
    }.get(opts.get("timestamp_position"), (f"{pad}", f"{pad}"))
    font = find_font()
    parts = ["drawtext=" + (f"fontfile={font}:" if font else ""),
             f"textfile={textfile}:expansion=normal:fontsize={size}:fontcolor=white",
             f":box=1:boxcolor=black@0.5:boxborderw={max(4, size // 4)}:x={pos[0]}:y={pos[1]}"]
    return "".join(parts)


def auto_bitrate(width: int, height: int, fps: float) -> int:
    return int(max(2, min(40, round(width * height * fps * 0.07 / 1_000_000))))


# --------------------------------------------------------------------------- #
# Audio (PulseAudio vom Home-Assistant-Audio-Plugin)
# --------------------------------------------------------------------------- #
def pulse_sources() -> list[str]:
    rc, out = run(["pactl", "list", "short", "sources"], timeout=10)
    if rc == 127:
        log("info", "pactl fehlt – nutze die im Audio-Tab der App gewählte Standardquelle")
        return ["default"]
    if rc != 0:
        log("warning", f"PulseAudio nicht erreichbar: {out.strip()[:200]}")
        return []
    sources = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and not parts[1].endswith(".monitor"):
            sources.append(parts[1])
    return sources


def pulse_default_source() -> str:
    rc, out = run(["pactl", "get-default-source"], timeout=10)
    if rc == 0 and out.strip() and "\n" not in out.strip():
        return out.strip()
    _, out = run(["pactl", "info"], timeout=10)
    m = re.search(r"Default Source:\s*(\S+)", out)
    return m.group(1) if m else ""


def pick_audio_source(option: str, sources: list[str], default: str, card_name: str) -> str | None:
    if not sources:
        return None
    if option:
        if option in sources:
            return option
        log("warning", f"Audioquelle '{option}' nicht gefunden – wähle automatisch")
    tokens = [t for t in re.split(r"[^a-z0-9]+", card_name.lower()) if len(t) >= 3 and t not in ("usb", "camera", "video")]
    for src in sources:
        if tokens and any(t in src.lower() for t in tokens) and "usb" in src.lower():
            return src
    if default in sources and "usb" in default.lower():
        return default
    usb = [s for s in sources if "usb" in s.lower()]
    if usb:
        return usb[0]
    return default if default in sources else sources[0]


def audio_input_args(source: str, caps: FFmpegCaps, env: dict) -> list[str]:
    if caps.has_demuxer("pulse"):
        return ["-f", "pulse", "-thread_queue_size", "1024", "-i", source]
    if source != "default":
        env["PULSE_SOURCE"] = source  # ALSA-Pulse-Plugin nutzt diese Quelle
    return ["-f", "alsa", "-thread_queue_size", "1024", "-i", "pulse"]


def test_audio(args: list[str], env: dict) -> tuple[bool, str]:
    rc, out = run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args, "-t", "1", "-f", "null", "-"],
                  timeout=15, env=env)
    return rc == 0, out.strip()[-300:]


# --------------------------------------------------------------------------- #
# ffmpeg-Befehl + go2rtc-Konfiguration
# --------------------------------------------------------------------------- #
def video_input_args(device: str, mode: dict, encoder: str) -> list[str]:
    args = []
    if encoder == "vaapi":
        args += ["-init_hw_device", f"vaapi=va:{render_node()}", "-filter_hw_device", "va"]
    args += ["-f", "v4l2", "-thread_queue_size", "512",
             "-input_format", FOURCC_TO_FFMPEG[mode["fourcc"]],
             "-video_size", f"{mode['width']}x{mode['height']}",
             "-framerate", f"{mode['fps']:g}", "-i", device]
    return args


def build_ffmpeg_command(video_in: list[str], audio_in: list[str] | None, encoder: str,
                         in_w: int, in_h: int, out_w: int, out_h: int, fps: float,
                         bitrate: int, opus: bool, overlay: str | None = None) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", *video_in]
    if audio_in:
        cmd += audio_in
    cmd += ["-map", "0:v:0"]
    scale = (out_w, out_h) != (in_w, in_h)
    gop = str(max(1, round(fps)))
    rate = ["-b:v", f"{bitrate}M", "-maxrate", f"{bitrate}M", "-bufsize", f"{bitrate * 2}M"]

    ov = f"{overlay}," if overlay else ""
    if encoder == "copy":
        cmd += ["-c:v", "copy"]
    elif encoder == "vaapi":
        # drawtext ist ein CPU-Filter -> vor dem Hochladen auf die GPU anwenden
        vf = ov + "format=nv12,hwupload" + (f",scale_vaapi=w={out_w}:h={out_h}" if scale else "")
        cmd += ["-vf", vf, "-c:v", "h264_vaapi", "-bf", "0", "-g", gop, *rate]
    elif encoder == "v4l2m2m":
        vf = (f"scale={out_w}:{out_h}:flags=fast_bilinear," if scale else "") + ov + "format=yuv420p"
        cmd += ["-vf", vf, "-c:v", "h264_v4l2m2m", "-num_output_buffers", "32",
                "-num_capture_buffers", "16", "-g", gop, *rate, "-bsf:v", "dump_extra=freq=keyframe"]
    else:
        vf = (f"scale={out_w}:{out_h}:flags=fast_bilinear," if scale else "") + ov + "format=yuv420p"
        cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-profile:v", "high", "-g", gop, "-keyint_min", gop, "-sc_threshold", "0", *rate]

    if audio_in:
        # Spur 1: AAC (für HLS / Aufnahmen), Spur 2: Opus (für WebRTC im Browser/App)
        cmd += ["-map", "1:a:0"]
        if opus:
            cmd += ["-map", "1:a:0"]
        cmd += ["-c:a:0", "aac", "-b:a:0", "128k"]
        if opus:
            cmd += ["-c:a:1", "libopus", "-b:a:1", "64k"]
        cmd += ["-ar", "48000", "-ac", "2"]
    cmd += ["-f", "rtsp", "-rtsp_transport", "tcp", "{output}"]
    return cmd


def build_mjpeg_command(device: str, mode: dict, overlay: str | None, out_fps: float) -> list[str]:
    """MJPEG wie MotionEye: Bilder der Kamera direkt weiterreichen (kaum CPU-Last).
    Nur mit Zeitstempel bzw. reduzierter Bildrate wird dekodiert und neu kodiert."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
           *video_input_args(device, mode, "software")]
    filters = []
    if out_fps + 0.5 < mode["fps"]:
        filters.append(f"fps={out_fps:g}")
    if overlay:
        filters.append(overlay)
    if filters:
        cmd += ["-vf", ",".join(filters), "-c:v", "mjpeg", "-q:v", "5", "-threads", "0"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-an", "-f", "mjpeg", "-"]  # Pipe zu go2rtc (stdout)
    return cmd


def to_exec_source(cmd: list[str]) -> str:
    parts = []
    for arg in cmd:
        parts.append(f'"{arg}"' if (" " in arg and not arg.startswith('"')) else arg)
    return "exec:" + " ".join(parts) + "#killsignal=15#killtimeout=5"


def build_go2rtc_config(opts: dict, source: str) -> dict:
    level = {"debug": "debug", "info": "info", "warning": "warn", "error": "error"}.get(opts["log_level"], "info")
    rtsp: dict = {"listen": f":{RTSP_PORT}"}
    if opts.get("rtsp_username") and opts.get("rtsp_password"):
        rtsp["username"] = opts["rtsp_username"]
        rtsp["password"] = opts["rtsp_password"]
    config = {
        "log": {"level": level, "format": "text"},
        "api": {"listen": f":{API_PORT}", "origin": "*"},
        "rtsp": rtsp,
        "webrtc": {"listen": f":{WEBRTC_PORT}"},
        "streams": {opts["stream_name"]: [source]},
    }
    if opts.get("always_on"):
        config["preload"] = {opts["stream_name"]: None}
    return config


# --------------------------------------------------------------------------- #
# Home Assistant: Generic-Camera automatisch anlegen
# --------------------------------------------------------------------------- #
def supervisor_request(method: str, path: str, body: dict | None = None, timeout: float = 90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SUPERVISOR_URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "null"
            return json.loads(raw)
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {path} -> HTTP {err.code}: {detail}") from None


def ha_api(method: str, path: str, body: dict | None = None):
    return supervisor_request(method, "/core/api" + path, body)


def own_address() -> str:
    """Interne IP der App im Home-Assistant-Netz.

    Bewusst die IP statt des Hostnamens: Der in Home Assistant eingebaute
    go2rtc/ffmpeg (WebRTC) kann App-Hostnamen nicht immer auflösen
    ("Name has no usable address"). Ändert sich die IP nach einem Neustart,
    aktualisiert ensure_registration() die Kamera automatisch.
    """
    if TOKEN:
        try:
            data = supervisor_request("GET", "/addons/self/info", timeout=15)
            ip = (data.get("data") or {}).get("ip_address")
            if ip and ip not in ("0.0.0.0", "127.0.0.1"):
                return ip
        except Exception as err:  # noqa: BLE001
            log("debug", f"IP über Supervisor nicht ermittelbar: {err}")
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return socket.gethostname()


def fill_schema(schema: list, values: dict) -> dict:
    """Füllt ein serialisiertes Flow-Formular (inkl. einklappbarer Sektionen)."""
    out: dict = {}
    for field in schema or []:
        name = field.get("name")
        if not name:
            continue
        if field.get("type") == "expandable":
            out[name] = fill_schema(field.get("schema", []), values)
            continue
        if name in values and values[name] is not None:
            out[name] = values[name]
            continue
        if name == "password":
            continue
        suggested = (field.get("description") or {}).get("suggested_value")
        if suggested is not None:
            out[name] = suggested
        elif "default" in field:
            out[name] = field["default"]
    return out


def run_flow(start_path: str, start_body: dict, step_prefix: str, values: dict) -> dict:
    flow = ha_api("POST", start_path, start_body)
    submitted = set()
    for _ in range(8):
        ftype = flow.get("type")
        if ftype != "form":
            return flow
        step = flow.get("step_id")
        if flow.get("errors"):
            try:
                ha_api("DELETE", step_prefix + flow["flow_id"])
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"Home Assistant meldet im Schritt '{step}': {flow['errors']}")
        if step in submitted:
            raise RuntimeError(f"Flow hängt im Schritt '{step}'")
        submitted.add(step)
        user_input = fill_schema(flow.get("data_schema", []), values)
        log("debug", f"Flow-Schritt {step}: {json.dumps(user_input)}")
        flow = ha_api("POST", step_prefix + flow["flow_id"], user_input)
    raise RuntimeError("Flow hat zu viele Schritte")


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def ws_rename(entry_id: str, title: str) -> None:
    try:
        import websocket  # py3-websocket-client
    except ImportError:
        log("warning", "websocket-client fehlt – Entität wird nicht umbenannt")
        return
    url = SUPERVISOR_URL.replace("http", "ws", 1) + "/core/websocket"
    ws = websocket.create_connection(url, timeout=30)
    try:
        json.loads(ws.recv())
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("WebSocket-Anmeldung fehlgeschlagen")
        counter = [0]

        def call(payload: dict) -> dict:
            counter[0] += 1
            payload["id"] = counter[0]
            ws.send(json.dumps(payload))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == counter[0] and msg.get("type") == "result":
                    return msg

        call({"type": "config_entries/update", "entry_id": entry_id, "title": title})
        entity_id = None
        for _ in range(20):
            res = call({"type": "config/entity_registry/list"})
            for ent in res.get("result") or []:
                if ent.get("config_entry_id") == entry_id and ent.get("entity_id", "").startswith("camera."):
                    entity_id = ent["entity_id"]
                    break
            if entity_id:
                break
            time.sleep(2)
        if not entity_id:
            log("warning", "Kamera-Entität nicht gefunden – Umbenennen übersprungen")
            return
        new_id = "camera." + slugify(title)
        payload = {"type": "config/entity_registry/update", "entity_id": entity_id, "name": title}
        if new_id != entity_id:
            payload["new_entity_id"] = new_id
        res = call(payload)
        if not res.get("success") and "new_entity_id" in payload:
            log("warning", f"{new_id} ist schon vergeben – behalte {entity_id}")
            payload.pop("new_entity_id")
            payload["entity_id"] = entity_id
            res = call(payload)
            new_id = entity_id
        if res.get("success"):
            log("info", f"Kamera-Entität: {new_id} ('{title}')")
    finally:
        ws.close()


INTEGRATION_LABEL = {"generic": "Generic Camera", "mjpeg": "MJPEG IP Camera"}


def registration_spec(stream_mode: str, opts: dict, host: str, cred: str) -> tuple[str, dict, dict]:
    """(Integration, Formularwerte, zu merkende URLs) je nach Modus."""
    name = opts["stream_name"]
    still = f"http://{host}:{API_PORT}/api/frame.jpeg?src={name}"
    if stream_mode == "mjpeg":
        mjpeg = f"http://{host}:{API_PORT}/api/stream.mjpeg?src={name}"
        values = {"name": opts["camera_name"], "mjpeg_url": mjpeg, "still_image_url": still,
                  "username": "", "password": "", "verify_ssl": False}
        return "mjpeg", values, {"mjpeg_url": mjpeg, "still_image_url": still}
    stream = f"rtsp://{cred}{host}:{RTSP_PORT}/{name}?video&audio=all"
    values = {"still_image_url": still, "stream_source": stream, "framerate": 2, "verify_ssl": False,
              "rtsp_transport": "tcp", "authentication": "basic", "confirmed_ok": True}
    return "generic", values, {"still_image_url": still, "stream_source": stream}


def ensure_registration(opts: dict, domain: str, values: dict, desired: dict) -> None:
    state = load_state()
    entries = {e.get("entry_id"): e.get("domain") for e in (ha_api("GET", "/config/config_entries/entry") or [])}
    entry_id = state.get("entry_id")
    old_domain = state.get("domain", "generic")  # bis 1.1.0 gab es nur Generic Camera
    label = INTEGRATION_LABEL.get(domain, domain)

    if entry_id in entries and entries[entry_id] == domain and old_domain == domain:
        if state.get("urls") == desired:
            log("info", f"Kamera ist bereits in Home Assistant eingerichtet ({label})")
            return
        log("info", "Stream-Adresse hat sich geändert – aktualisiere die Kamera in Home Assistant")
        run_flow("/config/config_entries/options/flow", {"handler": entry_id},
                 "/config/config_entries/options/flow/", values)
        state["urls"] = desired
        save_state(state)
        return

    if entry_id in entries:
        # Moduswechsel (z. B. H.264 -> MJPEG): alten, von der App angelegten Eintrag ersetzen
        log("info", f"Modus geändert – ersetze {INTEGRATION_LABEL.get(entries[entry_id], entries[entry_id])} "
                    f"durch {label} (Entität behält ihren Namen)")
        ha_api("DELETE", f"/config/config_entries/entry/{entry_id}")
        time.sleep(2)
    elif entry_id:
        log("info", "Kamera-Eintrag wurde in Home Assistant gelöscht – lege ihn neu an "
                    "(auto_register ausschalten, falls unerwünscht)")
    log("info", f"Lege Kamera in Home Assistant an ({label}) …")
    before = set(entries)
    result = run_flow("/config/config_entries/flow", {"handler": domain, "show_advanced_options": False},
                      "/config/config_entries/flow/", values)
    if result.get("type") == "abort" and result.get("reason") == "already_configured":
        log("info", f"Diese Kamera ist in Home Assistant bereits als {label} vorhanden")
        return
    if result.get("type") != "create_entry":
        raise RuntimeError(f"Einrichtung abgebrochen: {result.get('reason') or result}")
    new_id = (result.get("result") or {}).get("entry_id") if isinstance(result.get("result"), dict) else None
    if not new_id:
        after = ha_api("GET", f"/config/config_entries/entry?domain={domain}") or []
        fresh = [e for e in after if e.get("entry_id") not in before]
        new_id = fresh[0]["entry_id"] if fresh else None
    if not new_id:
        raise RuntimeError("Eintrag angelegt, aber ID nicht gefunden")
    save_state({"entry_id": new_id, "domain": domain, "urls": desired})
    log("info", "Kamera erfolgreich in Home Assistant angelegt")
    try:
        ws_rename(new_id, opts["camera_name"])
    except Exception as err:  # noqa: BLE001
        log("warning", f"Umbenennen fehlgeschlagen ({err}) – Name kann in HA manuell geändert werden")


def wait_for_go2rtc(timeout: float = 60) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api", timeout=3):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return False


def registration_worker(opts: dict, domain: str, values: dict, desired: dict) -> None:
    if not wait_for_go2rtc():
        log("error", "go2rtc API antwortet nicht – keine automatische Einrichtung")
        return
    for attempt in range(1, 6):
        try:
            ensure_registration(opts, domain, values, desired)
            return
        except Exception as err:  # noqa: BLE001
            log("warning", f"Automatische Einrichtung fehlgeschlagen (Versuch {attempt}/5): {err}")
            time.sleep(20 * attempt)
    log("error", "Automatische Einrichtung aufgegeben – Kamera bitte manuell anlegen (siehe Doku)")


def cleanup_legacy_options() -> None:
    """Alte Option "video_device" aus den gespeicherten Optionen entfernen (sonst Warnung im Supervisor-Log)."""
    try:
        info = supervisor_request("GET", "/addons/self/info", timeout=15).get("data") or {}
        stored = dict(info.get("options") or {})
        if "video_device" not in stored:
            return
        stored.pop("video_device")
        supervisor_request("POST", "/addons/self/options", {"options": stored}, timeout=15)
        log("debug", "Alte Option video_device entfernt")
    except Exception as err:  # noqa: BLE001
        log("debug", f"Alte Option nicht entfernt: {err}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def camera_busy(device: str) -> bool:
    """Eine USB-Kamera kann nur ein Programm gleichzeitig nutzen (z. B. MotionEye)."""
    rc, out = run(["v4l2-ctl", "-d", device, "--stream-mmap", "--stream-count=1", "--stream-to=/dev/null"],
                  timeout=15)
    return rc != 0 and ("busy" in out.lower() or "ebusy" in out.lower())


def apply_controls(device: str, controls: list[str]) -> None:
    for ctrl in controls or []:
        ctrl = str(ctrl).strip()
        if not ctrl or "=" not in ctrl:
            continue
        rc, out = run(["v4l2-ctl", "-d", device, "-c", ctrl], timeout=10)
        if rc == 0:
            log("info", f"Kamera-Einstellung gesetzt: {ctrl}")
        else:
            log("warning", f"Kamera-Einstellung {ctrl} nicht möglich: {out.strip()[:150]}")


def setup_audio(opts: dict, device: dict, caps: FFmpegCaps, env: dict) -> list[str] | None:
    if not opts["audio"]:
        return None
    opus = caps.has_encoder("libopus")
    sources = pulse_sources()
    log("info", f"Audioquellen: {', '.join(sources) or 'keine'}")
    src = pick_audio_source(opts["audio_source"], sources, pulse_default_source(), device["card"])
    if not src:
        log("warning", "Kein Mikrofon gefunden – Stream ohne Ton. Tipp: im Tab "
                       "'Konfiguration' > 'Audio' der App das Kamera-Mikrofon als Eingang wählen.")
        return None
    args = audio_input_args(src, caps, env)
    ok, err = test_audio(args, env)
    if not ok:
        log("warning", f"Mikrofon {src} nicht nutzbar ({err}) – Stream ohne Ton")
        return None
    log("info", f"Ton: {src} -> AAC" + (" + Opus" if opus else ""))
    return args


def log_timestamp(opts: dict) -> None:
    now = time.strftime("%d.%m.%Y %H:%M:%S")
    log("info", f"Zeitstempel eingeblendet ({opts.get('timestamp_position')}), Zeitzone "
                f"{os.environ.get('TZ') or time.tzname[0]} – aktuell {now}")


def plan_h264(opts: dict, device: dict, caps: FFmpegCaps, resolution: str):
    overlay_on = bool(opts.get("timestamp"))
    # Mit Einblendung wird ohnehin neu kodiert -> MJPEG bevorzugen (billiger zu dekodieren)
    mode_enc = "software" if overlay_on and opts["encoder"] in ("auto", "copy") else opts["encoder"]
    m = choose_mode(device["formats"], resolution, opts["fps"], opts["input_format"], mode_enc)
    res = parse_resolution(opts["output_resolution"]) if opts["output_resolution"] != "source" else None
    ow, oh = res or (m["width"], m["height"])
    if ow * oh > m["width"] * m["height"]:
        ow, oh = m["width"], m["height"]
    ow, oh = ow - ow % 2, oh - oh % 2
    br = int(opts["bitrate_mbps"]) or auto_bitrate(ow, oh, m["fps"])
    enc, fast = choose_encoder(opts["encoder"], m, ow, oh, caps, br, overlay_on)
    return m, ow, oh, br, enc, fast


def setup_h264(opts: dict, device: dict, caps: FFmpegCaps, env: dict, plan=None) -> str:
    mode, out_w, out_h, bitrate, encoder, fast = plan or plan_h264(opts, device, caps, opts["resolution"])
    if (not fast and opts["resolution"] == "auto" and opts["output_resolution"] == "source"
            and mode["width"] * mode["height"] > 1920 * 1080):
        log("warning", f"{mode['width']}x{mode['height']} ist für dieses Gerät zu viel – "
                       "wechsle automatisch auf 1080p (mit resolution/output_resolution änderbar)")
        mode, out_w, out_h, bitrate, encoder, fast = plan_h264(opts, device, caps, "1920x1080")
    if not fast:
        log("warning", "Stream wird ruckeln/verzögern! Abhilfe: stream_mode mjpeg, resolution verkleinern "
                       "(z. B. 1080p/720p), fps reduzieren oder Zeitstempel ausschalten.")
    log("info", f"Kameramodus: {mode['fourcc']} {mode['width']}x{mode['height']} @ {mode['fps']:g} fps")
    log("info", f"Video: {encoder} -> H.264 {out_w}x{out_h} @ {mode['fps']:g} fps"
                + ("" if encoder == "copy" else f", {bitrate} Mbit/s"))
    overlay = None
    if opts.get("timestamp"):
        # Bei VAAPI wird vor dem Skalieren gezeichnet (Eingangsgröße), sonst danach
        draw_h = mode["height"] if encoder == "vaapi" else out_h
        overlay = build_timestamp_filter(opts, draw_h, TIMESTAMP_FILE)
        log_timestamp(opts)
    audio_in = setup_audio(opts, device, caps, env)
    video_in = video_input_args(device["path"], mode, encoder)
    cmd = build_ffmpeg_command(video_in, audio_in, encoder, mode["width"], mode["height"],
                               out_w, out_h, mode["fps"], bitrate, caps.has_encoder("libopus"), overlay)
    return to_exec_source(cmd)


def setup_mjpeg(opts: dict, device: dict, mjpeg_formats: dict) -> str:
    mode = choose_mode(mjpeg_formats, opts["resolution"], opts["fps"], "auto", "software")
    w, h = mode["width"], mode["height"]
    out_fps = mode["fps"]
    overlay = None
    if opts.get("timestamp"):
        overlay = build_timestamp_filter(opts, h, TIMESTAMP_FILE)
        log("info", f"Messe, wie schnell der Zeitstempel bei {w}x{h} eingeblendet werden kann …")
        speed = benchmark_mjpeg_overlay(w, h, overlay)
        if speed and speed < mode["fps"] * 0.95:
            # Beste Kombination aus Kamera-fps und ausgegebenen fps suchen. Dekodiert wird jedes
            # Kamerabild (~1/3 der Arbeit), kodiert nur die ausgegebenen (~2/3).
            fps_list = mjpeg_formats.get(mode["fourcc"], {}).get((w, h), []) or [mode["fps"]]
            best = (0.0, min(fps_list))
            for cam in sorted(fps_list):
                if cam <= mode["fps"] + 0.5:
                    out = cam if cam <= speed * 0.85 else float(int((speed * 0.85 - cam / 3) * 1.5))
                    out = min(out, cam)
                    if out > best[0]:
                        best = (out, cam)
            if best[0] >= 1:
                out_fps, cam_fps = best
            else:  # Kamera kann nicht langsamer -> stark drosseln
                out_fps, cam_fps = max(1.0, float(int(speed * 0.5))), min(fps_list)
            mode = dict(mode, fps=cam_fps)
            log("warning", f"Zeitstempel bei {w}x{h}: dieses Gerät schafft ca. {speed:.0f} fps – Stream läuft "
                           f"mit {out_fps:g} fps. Für volle {opts['fps']} fps: timestamp aus (dann ohne "
                           "Neukodierung) oder resolution verkleinern.")
        elif speed:
            log("info", f"Zeitstempel bei {w}x{h}: ca. {speed:.0f} fps möglich")
        log_timestamp(opts)
    if opts["output_resolution"] != "source":
        log("info", "output_resolution gilt nur im H.264-Modus – im MJPEG-Modus resolution verwenden")
    if opts["audio"]:
        log("info", "MJPEG-Modus: Bild ohne Ton (wie bei MotionEye). Ton gibt es mit stream_mode: h264")
    log("info", f"Kameramodus: {mode['fourcc']} {w}x{h} @ {mode['fps']:g} fps")
    log("info", f"Video: MJPEG {w}x{h} @ {out_fps:g} fps "
                + ("(neu kodiert mit Zeitstempel)" if overlay else "(direkt von der Kamera, keine Last)"))
    return to_exec_source(build_mjpeg_command(device["path"], mode, overlay, out_fps))


def setup_stream(opts: dict, device: dict, caps: FFmpegCaps, env: dict) -> tuple[str, str]:
    """Wählt MJPEG oder H.264 und liefert (modus, go2rtc-Quelle)."""
    mjpeg_formats = {f: sz for f, sz in device["formats"].items() if f in ("MJPG", "JPEG") and sz}
    want = opts["stream_mode"]
    if want == "mjpeg" and not mjpeg_formats:
        log("warning", "Kamera liefert kein MJPEG – nutze H.264-Modus")
        want = "h264"
    plan = None
    if want == "auto":
        if not mjpeg_formats:
            want = "h264"
        else:
            plan = plan_h264(opts, device, caps, opts["resolution"])
            m, ow, oh, _br, enc, fast = plan
            if fast and enc != "v4l2m2m":
                want = "h264"
                log("info", f"Modus: H.264 ({enc}) – Gerät schafft {ow}x{oh} @ {m['fps']:g} fps, mit Ton")
            else:
                want = "mjpeg"
                log("info", "Modus: MJPEG (wie MotionEye) – H.264-Kodierung in voller Auflösung ist auf "
                            "diesem Gerät nicht flüssig möglich. Mit stream_mode: h264 erzwingbar.")
    if want == "mjpeg":
        return "mjpeg", setup_mjpeg(opts, device, mjpeg_formats)
    return "h264", setup_h264(opts, device, caps, env, plan)


def main() -> int:
    global _log_level
    opts = load_options()
    _log_level = _LEVELS.get(opts["log_level"], 20)
    env = os.environ.copy()

    if opts["custom_ffmpeg_command"].strip():
        custom = opts["custom_ffmpeg_command"].strip()
        if "{output}" not in custom:
            log("error", "custom_ffmpeg_command muss {output} enthalten (Ziel: -f rtsp {output})")
            return 1
        source = "exec:" + custom + "#killsignal=15#killtimeout=5"
        stream_mode = "h264"
        log("info", "Nutze eigenen ffmpeg-Befehl (automatische Erkennung übersprungen)")
    else:
        nodes = list_video_nodes()
        if not nodes:
            log("error", "Keine /dev/video* Geräte gefunden. Ist die Kamera eingesteckt? "
                         "Nach dem Einstecken die App neu starten.")
            return 1
        probes = [probe_device(n) for n in nodes]
        for p in probes:
            fmt = ", ".join(
                f"{f}({max(p['formats'][f], key=lambda r: r[0] * r[1])[0]}x"
                f"{max(p['formats'][f], key=lambda r: r[0] * r[1])[1]})"
                for f in p["formats"] if p["formats"][f]
            ) or "-"
            log("info", f"Gefunden: {p['path']} [{p['driver'] or '?'}] {p['card'] or ''} – {fmt}")
        device = select_device(opts["camera_device"], probes)
        if not device:
            log("error", "Keine nutzbare Kamera gefunden (siehe Liste oben)")
            return 1
        log("info", f"Verwende Kamera {device['path']}: {device['card']}")
        if camera_busy(device["path"]):
            log("error", "=" * 70)
            log("error", f"Die Kamera {device['path']} wird gerade von einem anderen Programm benutzt "
                         "(z. B. MotionEye, Frigate, eine andere App). Eine USB-Kamera kann nur EIN Programm "
                         "gleichzeitig nutzen. Dort die Kamera entfernen bzw. die andere App stoppen und "
                         "danach diese App neu starten.")
            log("error", "=" * 70)
        for fcc, sizes in device["formats"].items():
            for (w, h), fpsl in sorted(sizes.items(), key=lambda x: -x[0][0] * x[0][1]):
                log("debug", f"  {fcc} {w}x{h} @ {', '.join(f'{f:g}' for f in fpsl)} fps")

        apply_controls(device["path"], opts["v4l2_controls"])
        caps = FFmpegCaps()
        stream_mode, source = setup_stream(opts, device, caps, env)

    config = build_go2rtc_config(opts, source)
    with open(GO2RTC_CONFIG, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)  # JSON ist gültiges YAML
    log("debug", f"go2rtc-Konfiguration:\n{json.dumps(config, indent=2)}")

    host = own_address()
    cred = ""
    if opts.get("rtsp_username") and opts.get("rtsp_password"):
        cred = (urllib.parse.quote(opts["rtsp_username"], safe="") + ":"
                + urllib.parse.quote(opts["rtsp_password"], safe="") + "@")
    domain, values, desired = registration_spec(stream_mode, opts, host, cred)
    for key, url in desired.items():
        log("info", f"{key:<16} (intern): {url}")

    proc = subprocess.Popen([GO2RTC_BIN, "-config", GO2RTC_CONFIG], env=env)

    def stop(signum, _frame):
        log("info", "Beende …")
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if TOKEN:
        threading.Thread(target=cleanup_legacy_options, daemon=True).start()
    if opts["auto_register"]:
        if TOKEN:
            threading.Thread(target=registration_worker, args=(opts, domain, values, desired), daemon=True).start()
        else:
            log("warning", "Kein SUPERVISOR_TOKEN – automatische Einrichtung nicht möglich")

    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
