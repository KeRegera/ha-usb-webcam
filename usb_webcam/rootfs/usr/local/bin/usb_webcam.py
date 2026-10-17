#!/usr/bin/env python3
"""USB Webcam – Home Assistant OS App.

Ablauf beim Start:
  1. USB-Kamera (V4L2) finden und ihre Formate/Auflösungen/FPS auslesen
  2. Bestes Eingangsformat + Encoder wählen (H.264-Passthrough, VAAPI, Raspberry-Pi-HW, Software)
  3. Mikrofon der Kamera über PulseAudio finden
  4. go2rtc mit einem ffmpeg-Stream (H.264 + AAC + Opus) starten
  5. Kamera einmalig als "Generic Camera"-Integration in Home Assistant anlegen
     -> normale camera.* Entität mit WebRTC (Ton, volle FPS) im Dashboard
"""

from __future__ import annotations

import glob
import json
import os
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
SUPERVISOR_URL = os.environ.get("USBCAM_SUPERVISOR_URL", "http://supervisor")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

API_PORT = 1984
RTSP_PORT = 8554
WEBRTC_PORT = 8555

DEFAULTS = {
    "video_device": "auto",
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


def select_device(option: str, probes: list[dict]) -> dict | None:
    if option and option != "auto":
        path = os.path.realpath(option)
        dev = next((p for p in probes if os.path.realpath(p["path"]) == path), None) or probe_device(path)
        if not dev["formats"]:
            log("error", f"{option} liefert keine Videoformate (falsches Gerät?)")
            return None
        return dev
    candidates = [p for p in probes if is_capture_node(p) and p["formats"]]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p["driver"] == "uvcvideo", max_pixels(p["formats"])), reverse=True)
    return candidates[0]


# --------------------------------------------------------------------------- #
# Modus- und Encoder-Wahl
# --------------------------------------------------------------------------- #
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


def benchmark_software(width: int, height: int, fps: float, bitrate: int) -> float:
    frames = max(30, int(round(fps)) * 3)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
           f"testsrc2=size={width}x{height}:rate={max(1, round(fps))}", "-frames:v", str(frames),
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
           "-b:v", f"{bitrate}M", "-f", "null", "-"]
    start = time.monotonic()
    rc, _ = run(cmd, timeout=120)
    elapsed = time.monotonic() - start
    return frames / elapsed if rc == 0 and elapsed > 0 else 0.0


def choose_encoder(opt: str, mode: dict, out_w: int, out_h: int, caps: FFmpegCaps, bitrate: int) -> str:
    same_size = (out_w, out_h) == (mode["width"], mode["height"])
    if mode["fourcc"] == "H264" and same_size and opt in ("auto", "copy"):
        return "copy"
    if opt == "copy":
        log("warning", "encoder=copy geht nur mit H.264-Kameraformat ohne Skalierung – wähle automatisch")
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
            return enc
        log("info" if opt == "auto" else "warning",
            f"Hardware-Encoder {enc} bei {out_w}x{out_h} nicht nutzbar{': ' + err if err else ''}")

    speed = benchmark_software(out_w, out_h, mode["fps"], bitrate)
    if speed and speed < mode["fps"] * 0.95:
        log("warning",
            f"CPU schafft Software-Kodierung bei {out_w}x{out_h} nur mit ca. {speed:.1f} fps "
            f"(gewünscht {mode['fps']:g}). Stream wird ruckeln/verzögern! Abhilfe: Kamera mit H.264-Ausgabe, "
            f"output_resolution verkleinern (z. B. 1080p) oder fps reduzieren.")
    elif speed:
        log("info", f"Software-Kodierung: ca. {speed:.0f} fps möglich bei {out_w}x{out_h}")
    return "software"


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
                         bitrate: int, opus: bool) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", *video_in]
    if audio_in:
        cmd += audio_in
    cmd += ["-map", "0:v:0"]
    scale = (out_w, out_h) != (in_w, in_h)
    gop = str(max(1, round(fps)))
    rate = ["-b:v", f"{bitrate}M", "-maxrate", f"{bitrate}M", "-bufsize", f"{bitrate * 2}M"]

    if encoder == "copy":
        cmd += ["-c:v", "copy"]
    elif encoder == "vaapi":
        vf = "format=nv12,hwupload" + (f",scale_vaapi=w={out_w}:h={out_h}" if scale else "")
        cmd += ["-vf", vf, "-c:v", "h264_vaapi", "-bf", "0", "-g", gop, *rate]
    elif encoder == "v4l2m2m":
        vf = (f"scale={out_w}:{out_h}:flags=fast_bilinear," if scale else "") + "format=yuv420p"
        cmd += ["-vf", vf, "-c:v", "h264_v4l2m2m", "-num_output_buffers", "32",
                "-num_capture_buffers", "16", "-g", gop, *rate]
    else:
        vf = (f"scale={out_w}:{out_h}:flags=fast_bilinear," if scale else "") + "format=yuv420p"
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


def own_hostname() -> str:
    try:
        data = supervisor_request("GET", "/addons/self/info", timeout=15)
        host = (data.get("data") or {}).get("hostname")
        if host:
            return host
    except Exception as err:  # noqa: BLE001
        log("debug", f"Hostname über Supervisor nicht ermittelbar: {err}")
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


def ensure_registration(opts: dict, still_url: str, stream_url: str) -> None:
    values = {
        "still_image_url": still_url,
        "stream_source": stream_url,
        "framerate": 2,
        "verify_ssl": False,
        "rtsp_transport": "tcp",
        "authentication": "basic",
        "confirmed_ok": True,
    }
    desired = {"still_image_url": still_url, "stream_source": stream_url}
    state = load_state()
    entries = ha_api("GET", "/config/config_entries/entry?domain=generic") or []
    entry_ids = {e.get("entry_id") for e in entries}
    entry_id = state.get("entry_id")

    if entry_id in entry_ids:
        if state.get("urls") == desired:
            log("info", "Kamera ist bereits in Home Assistant eingerichtet")
            return
        log("info", "Stream-Adresse hat sich geändert – aktualisiere die Kamera in Home Assistant")
        run_flow("/config/config_entries/options/flow", {"handler": entry_id},
                 "/config/config_entries/options/flow/", values)
        state["urls"] = desired
        save_state(state)
        return

    if entry_id:
        log("info", "Kamera-Eintrag wurde in Home Assistant gelöscht – lege ihn neu an "
                    "(auto_register ausschalten, falls unerwünscht)")
    log("info", "Lege Kamera in Home Assistant an (Generic Camera) …")
    result = run_flow("/config/config_entries/flow", {"handler": "generic", "show_advanced_options": False},
                      "/config/config_entries/flow/", values)
    if result.get("type") != "create_entry":
        raise RuntimeError(f"Einrichtung abgebrochen: {result.get('reason') or result}")
    new_id = (result.get("result") or {}).get("entry_id") if isinstance(result.get("result"), dict) else None
    if not new_id:
        after = ha_api("GET", "/config/config_entries/entry?domain=generic") or []
        fresh = [e for e in after if e.get("entry_id") not in entry_ids]
        new_id = fresh[0]["entry_id"] if fresh else None
    if not new_id:
        raise RuntimeError("Eintrag angelegt, aber ID nicht gefunden")
    save_state({"entry_id": new_id, "urls": desired})
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


def registration_worker(opts: dict, still_url: str, stream_url: str) -> None:
    if not wait_for_go2rtc():
        log("error", "go2rtc API antwortet nicht – keine automatische Einrichtung")
        return
    for attempt in range(1, 6):
        try:
            ensure_registration(opts, still_url, stream_url)
            return
        except Exception as err:  # noqa: BLE001
            log("warning", f"Automatische Einrichtung fehlgeschlagen (Versuch {attempt}/5): {err}")
            time.sleep(20 * attempt)
    log("error", "Automatische Einrichtung aufgegeben – Kamera bitte manuell anlegen (siehe Doku)")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
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
        device = select_device(opts["video_device"], probes)
        if not device:
            log("error", "Keine nutzbare Kamera gefunden (siehe Liste oben)")
            return 1
        log("info", f"Verwende Kamera {device['path']}: {device['card']}")
        for fcc, sizes in device["formats"].items():
            for (w, h), fpsl in sorted(sizes.items(), key=lambda x: -x[0][0] * x[0][1]):
                log("debug", f"  {fcc} {w}x{h} @ {', '.join(f'{f:g}' for f in fpsl)} fps")

        apply_controls(device["path"], opts["v4l2_controls"])
        mode = choose_mode(device["formats"], opts["resolution"], opts["fps"], opts["input_format"], opts["encoder"])
        log("info", f"Kameramodus: {mode['fourcc']} {mode['width']}x{mode['height']} @ {mode['fps']:g} fps")

        out_res = parse_resolution(opts["output_resolution"]) if opts["output_resolution"] != "source" else None
        out_w, out_h = out_res or (mode["width"], mode["height"])
        if out_w * out_h > mode["width"] * mode["height"]:
            out_w, out_h = mode["width"], mode["height"]
        out_w, out_h = out_w - out_w % 2, out_h - out_h % 2
        bitrate = int(opts["bitrate_mbps"]) or auto_bitrate(out_w, out_h, mode["fps"])

        caps = FFmpegCaps()
        encoder = choose_encoder(opts["encoder"], mode, out_w, out_h, caps, bitrate)
        log("info", f"Video: {encoder} -> H.264 {out_w}x{out_h} @ {mode['fps']:g} fps"
                    + ("" if encoder == "copy" else f", {bitrate} Mbit/s"))

        audio_in = None
        opus = caps.has_encoder("libopus")
        if opts["audio"]:
            sources = pulse_sources()
            log("info", f"Audioquellen: {', '.join(sources) or 'keine'}")
            src = pick_audio_source(opts["audio_source"], sources, pulse_default_source(), device["card"])
            if src:
                args = audio_input_args(src, caps, env)
                ok, err = test_audio(args, env)
                if ok:
                    audio_in = args
                    log("info", f"Ton: {src} -> AAC" + (" + Opus" if opus else ""))
                else:
                    log("warning", f"Mikrofon {src} nicht nutzbar ({err}) – Stream ohne Ton")
            else:
                log("warning", "Kein Mikrofon gefunden – Stream ohne Ton. Tipp: im Tab "
                               "'Konfiguration' > 'Audio' der App das Kamera-Mikrofon als Eingang wählen.")

        video_in = video_input_args(device["path"], mode, encoder)
        cmd = build_ffmpeg_command(video_in, audio_in, encoder, mode["width"], mode["height"],
                                   out_w, out_h, mode["fps"], bitrate, opus)
        source = to_exec_source(cmd)

    config = build_go2rtc_config(opts, source)
    with open(GO2RTC_CONFIG, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)  # JSON ist gültiges YAML
    log("debug", f"go2rtc-Konfiguration:\n{json.dumps(config, indent=2)}")

    host = own_hostname() if TOKEN else socket.gethostname()
    name = opts["stream_name"]
    cred = ""
    if opts.get("rtsp_username") and opts.get("rtsp_password"):
        cred = (urllib.parse.quote(opts["rtsp_username"], safe="") + ":"
                + urllib.parse.quote(opts["rtsp_password"], safe="") + "@")
    still_url = f"http://{host}:{API_PORT}/api/frame.jpeg?src={name}"
    stream_url = f"rtsp://{cred}{host}:{RTSP_PORT}/{name}?video&audio=all"
    log("info", f"RTSP-Stream (intern): rtsp://{host}:{RTSP_PORT}/{name}")
    log("info", f"Standbild (intern):   {still_url}")

    proc = subprocess.Popen([GO2RTC_BIN, "-config", GO2RTC_CONFIG], env=env)

    def stop(signum, _frame):
        log("info", "Beende …")
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if opts["auto_register"]:
        if TOKEN:
            threading.Thread(target=registration_worker, args=(opts, still_url, stream_url), daemon=True).start()
        else:
            log("warning", "Kein SUPERVISOR_TOKEN – automatische Einrichtung nicht möglich")

    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
