#!/usr/bin/env python3
"""USB Webcam – Home Assistant OS App (Version 2).

Prinzip wie MotionEye:
  * Die Kamera liefert MJPEG, das Bild wird direkt weitergereicht (volle 4K auch
    auf dem Raspberry Pi). Nur für den Zeitstempel wird neu berechnet.
  * go2rtc stellt den MJPEG-Stream und Standbilder bereit.
  * In Home Assistant wird die Kamera als "MJPEG IP Camera" angelegt
    -> ganz normale camera.* Entität.
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
import urllib.request

OPTIONS_PATH = os.environ.get("USBCAM_OPTIONS", "/data/options.json")
STATE_PATH = os.environ.get("USBCAM_STATE", "/data/state.json")
BENCH_PATH = os.environ.get("USBCAM_BENCH", "/data/benchmark.json")
GO2RTC_CONFIG = os.environ.get("USBCAM_GO2RTC_CONFIG", "/tmp/go2rtc.yaml")
GO2RTC_BIN = os.environ.get("USBCAM_GO2RTC_BIN", "go2rtc")
TIMESTAMP_FILE = os.environ.get("USBCAM_TIMESTAMP_FILE", "/tmp/usb_webcam_timestamp.txt")
SYSFS_V4L = os.environ.get("USBCAM_SYSFS_V4L", "/sys/class/video4linux")
SUPERVISOR_URL = os.environ.get("USBCAM_SUPERVISOR_URL", "http://supervisor")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

API_PORT = 1984
STREAM = "webcam"
OPTION_KEYS = {"device", "resolution", "fps", "timestamp", "timestamp_position", "camera_name"}
DEFAULTS = {
    "device": "",
    "resolution": "3840x2160",
    "fps": 30,
    "timestamp": True,
    "timestamp_position": "top_left",
    "camera_name": "USB Webcam",
}


def log(level: str, msg: str) -> None:
    print(f"[usb_webcam] {level.upper():<7} {msg}", flush=True)


def run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: nicht gefunden"
    except subprocess.TimeoutExpired:
        return 124, "Timeout"


def load_options() -> dict:
    opts = dict(DEFAULTS)
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as fh:
            opts.update({k: v for k, v in json.load(fh).items() if k in OPTION_KEYS and v is not None})
    except FileNotFoundError:
        log("warning", f"{OPTIONS_PATH} fehlt – nutze Standardwerte")
    opts["fps"] = max(1, int(opts["fps"]))
    opts["device"] = str(opts.get("device") or "").strip()
    return opts


def slugify(text: str) -> str:
    text = text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "usb_webcam"


# --------------------------------------------------------------------------- #
# Kamera finden
# --------------------------------------------------------------------------- #
def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def sysfs_nodes() -> dict[str, dict]:
    """/dev/videoN -> Infos aus /sys (Index, USB-Gerät, Seriennummer, Name)."""
    nodes = {}
    for entry in glob.glob(os.path.join(SYSFS_V4L, "video*")):
        iface = os.path.realpath(os.path.join(entry, "device"))
        usb = os.path.dirname(iface)
        nodes["/dev/" + os.path.basename(entry)] = {
            "index": _read(os.path.join(entry, "index")) or "0",
            "name": _read(os.path.join(entry, "name")),
            "iface": os.path.basename(iface),
            "usb": usb,
            "serial": _read(os.path.join(usb, "serial")),
            "product": _read(os.path.join(usb, "product")),
            "manufacturer": _read(os.path.join(usb, "manufacturer")),
        }
    return nodes


def resolve_device_path(option: str) -> str | None:
    """Auswahl aus der Liste (z. B. /dev/v4l/by-id/usb-…-video-index0) -> /dev/videoN.
    Die by-id-Verknüpfungen gibt es im Container nicht immer, dann über /sys auflösen."""
    if os.path.exists(option):
        return os.path.realpath(option)
    base = os.path.basename(option)
    m = re.search(r"-video-index(\d+)$", base)
    index = m.group(1) if m else None
    m_path = re.search(r"-usb(?:v\d)?-\d+:([\d.]+):(\d+\.\d+)-video-index\d+$", base)
    for dev, info in sorted(sysfs_nodes().items()):
        if index is not None and info["index"] != index:
            continue
        if m_path and info["iface"].endswith(f"-{m_path.group(1)}:{m_path.group(2)}"):
            return dev
        if info["serial"] and f"_{info['serial']}-video-index" in base:
            return dev
        label = re.sub(r"\s+", "_", f"{info['manufacturer']}_{info['product']}")
        if info["product"] and label in base:
            return dev
    return None


def parse_caps(info_text: str) -> list[str]:
    lines = info_text.splitlines()
    for key in ("device caps", "capabilities"):
        for idx, line in enumerate(lines):
            if line.strip().lower().startswith(key):
                base = len(line) - len(line.lstrip())
                caps = []
                for sub in lines[idx + 1:]:
                    if not sub.strip() or len(sub) - len(sub.lstrip()) <= base:
                        break
                    caps.append(sub.strip().lower())
                if caps:
                    return caps
    return []


def parse_formats(text: str) -> dict[str, dict[tuple[int, int], list[float]]]:
    """`v4l2-ctl --list-formats-ext` -> {FOURCC: {(w, h): [fps, ...]}}"""
    formats: dict[str, dict[tuple[int, int], list[float]]] = {}
    fourcc = size = None
    for line in text.splitlines():
        m = re.search(r"\[\d+\]:\s*'([^']+)'", line)
        if m:
            fourcc, size = m.group(1).strip().upper(), None
            formats.setdefault(fourcc, {})
            continue
        if fourcc is None:
            continue
        m = re.search(r"Size:\s*Discrete\s+(\d+)x(\d+)", line) or \
            re.search(r"Size:\s*(?:Stepwise|Continuous)\s+\d+x\d+\s*-\s*(\d+)x(\d+)", line)
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
    return {k: v for k, v in formats.items() if v}


def probe(path: str) -> dict:
    _, info = run(["v4l2-ctl", "-d", path, "--info"])
    _, fmts = run(["v4l2-ctl", "-d", path, "--list-formats-ext"])
    caps = parse_caps(info)
    m = re.search(r"Card type\s*:\s*(.+)", info)
    return {
        "path": path,
        "card": m.group(1).strip() if m else "",
        "capture": any(c in ("video capture", "video capture multiplanar") for c in caps)
                   and not any("memory-to-memory" in c for c in caps),
        "formats": parse_formats(fmts),
    }


def find_camera(option: str) -> dict | None:
    path = resolve_device_path(option) if option else None
    if option and not path:
        log("error", f"Das gewählte Gerät {option} wurde nicht gefunden. Ist die Kamera eingesteckt?")
        return None
    nodes = sysfs_nodes()
    if path and not re.fullmatch(r"/dev/video\d+", path):
        log("error", f"{option} ist keine Kamera. Bitte in der Liste den Eintrag "
                     "'/dev/v4l/by-id/usb-…-video-index0' deiner Webcam wählen.")
        return None
    if path:
        dev = probe(path)
        if dev["capture"] and dev["formats"]:
            return dev
        # z. B. "...-video-index1" gewählt: das ist nur der Metadaten-Knoten derselben Kamera
        usb = nodes.get(path, {}).get("usb")
        for other in sorted(nodes):
            if other != path and usb and nodes[other].get("usb") == usb:
                cand = probe(other)
                if cand["capture"] and cand["formats"]:
                    log("warning", f"{path} liefert kein Bild – nehme {other} derselben Kamera "
                                   "(Tipp: in der Liste den Eintrag mit 'index0' wählen)")
                    return cand
        log("error", f"{path} liefert kein Videobild. Bitte den Eintrag mit 'video-index0' wählen.")
        return None
    return None


# --------------------------------------------------------------------------- #
# Modus, Zeitstempel, ffmpeg
# --------------------------------------------------------------------------- #
def choose_mode(formats: dict, resolution: str, fps_wanted: int) -> dict:
    """Bevorzugt MJPEG (wie MotionEye). Exakte Auflösung, sonst die nächstkleinere."""
    m = re.fullmatch(r"(\d+)x(\d+)", resolution)
    want = (int(m.group(1)), int(m.group(2))) if m else (3840, 2160)
    for fourcc in ("MJPG", "JPEG", "YUYV", "NV12", "YU12", "UYVY"):
        sizes = formats.get(fourcc)
        if not sizes:
            continue
        fits = [s for s in sizes if s[0] * s[1] <= want[0] * want[1]]
        size = max(fits, key=lambda s: s[0] * s[1]) if fits else min(sizes, key=lambda s: s[0] * s[1])
        fps_list = sizes[size]
        ok = [f for f in fps_list if f <= fps_wanted + 0.5]
        fps = max(ok) if ok else (min(fps_list) if fps_list else float(fps_wanted))
        return {"fourcc": fourcc, "width": size[0], "height": size[1], "fps": fps, "fps_list": fps_list}
    raise RuntimeError(f"Kamera liefert kein unterstütztes Format: {list(formats)}")


INPUT_FORMAT = {"MJPG": "mjpeg", "JPEG": "mjpeg", "YUYV": "yuyv422", "NV12": "nv12", "YU12": "yuv420p",
                "UYVY": "uyvy422"}


def input_args(device: str, mode: dict) -> list[str]:
    return ["-f", "v4l2", "-input_format", INPUT_FORMAT[mode["fourcc"]],
            "-video_size", f"{mode['width']}x{mode['height']}", "-framerate", f"{mode['fps']:g}", "-i", device]


def find_font() -> str | None:
    for pattern in ("/usr/share/fonts/**/DejaVuSans-Bold.ttf", "/usr/share/fonts/**/*.ttf"):
        hits = sorted(glob.glob(pattern, recursive=True))
        if hits:
            return hits[0]
    return None


def timestamp_filter(position: str, frame_h: int) -> str:
    # Text aus Datei -> keine Sonderzeichen im ffmpeg-Befehl
    with open(TIMESTAMP_FILE, "w", encoding="utf-8") as fh:
        fh.write("%{localtime:%d.%m.%Y %H\\:%M\\:%S}")
    size = max(14, round(frame_h / 30))
    pad = max(8, round(frame_h / 60))
    x, y = {"top_left": (f"{pad}", f"{pad}"), "top_right": (f"w-tw-{pad}", f"{pad}"),
            "bottom_left": (f"{pad}", f"h-th-{pad}"), "bottom_right": (f"w-tw-{pad}", f"h-th-{pad}")
            }.get(position, (f"{pad}", f"{pad}"))
    font = find_font()
    return ("drawtext=" + (f"fontfile={font}:" if font else "")
            + f"textfile={TIMESTAMP_FILE}:expansion=normal:fontsize={size}:fontcolor=white"
            + f":box=1:boxcolor=black@0.5:boxborderw={max(4, size // 4)}:x={x}:y={y}")


def frame_has_huffman(device: str, mode: dict) -> bool | None:
    """Manche Webcams lassen in ihren MJPEG-Bildern die Huffman-Tabellen weg – dann zeigen
    Browser/iPhone kein Bild. Ein Bild holen und prüfen."""
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *input_args(device, mode),
                               "-frames:v", "1", "-c:v", "copy", "-f", "mjpeg", "-"],
                              capture_output=True, timeout=20, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0 or not proc.stdout.startswith(b"\xff\xd8"):
        return None
    return b"\xff\xc4" in proc.stdout


def cached_measure(key: str, fn) -> float:
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
                json.dump(data, fh)
        except OSError:
            pass
    return value


def measure_reencode(width: int, height: int, vf: str | None) -> float:
    """Bilder pro Sekunde, die dieses Gerät dekodieren + Zeitstempel + kodieren kann."""
    def measure() -> float:
        sample = f"/tmp/usb_webcam_bench_{width}x{height}.mjpeg"
        rc, _ = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                     f"testsrc2=size={width}x{height}:rate=10", "-frames:v", "10", "-pix_fmt", "yuvj422p",
                     "-c:v", "mjpeg", "-q:v", "5", sample], timeout=120)
        if rc != 0:
            return 0.0
        start = time.monotonic()
        rc, _ = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-stream_loop", "2", "-i", sample,
                     *(["-vf", vf] if vf else []), "-c:v", "mjpeg", "-q:v", "5", "-threads", "0",
                     "-f", "null", "-"], timeout=180)
        elapsed = time.monotonic() - start
        try:
            os.remove(sample)
        except OSError:
            pass
        return 30 / elapsed if rc == 0 and elapsed > 0 else 0.0

    return cached_measure(f"reencode:{width}x{height}:{bool(vf)}", measure)


def plan_fps(speed: float, mode: dict) -> tuple[float, float]:
    """(Kamera-fps, Ausgabe-fps): dekodiert wird jedes Kamerabild (~1/3 der Arbeit),
    kodiert nur die ausgegebenen (~2/3)."""
    fps_list = [f for f in (mode["fps_list"] or [mode["fps"]]) if f <= mode["fps"] + 0.5] or [mode["fps"]]
    best = (0.0, min(fps_list))
    for cam in sorted(fps_list):
        out = cam if cam <= speed * 0.85 else float(int((speed * 0.85 - cam / 3) * 1.5))
        out = min(out, cam)
        if out > best[0]:
            best = (out, cam)
    if best[0] >= 1:
        return best[1], best[0]
    return min(fps_list), max(1.0, float(int(speed * 0.5)))


def build_source(opts: dict, cam: dict) -> str:
    mode = choose_mode(cam["formats"], opts["resolution"], opts["fps"])
    w, h = mode["width"], mode["height"]
    if f"{w}x{h}" != opts["resolution"]:
        log("warning", f"Die Kamera kann {opts['resolution']} nicht – nehme {w}x{h}")
    is_mjpeg = mode["fourcc"] in ("MJPG", "JPEG")

    reencode, reason = False, ""
    if not is_mjpeg:
        reencode, reason = True, f"Kamera liefert {mode['fourcc']} statt MJPEG"
    elif opts["timestamp"]:
        reencode, reason = True, "Zeitstempel"
    else:
        dht = frame_has_huffman(cam["path"], mode)
        if dht is False:
            reencode, reason = True, "Kamerabilder ohne Huffman-Tabellen (sonst zeigen Browser kein Bild)"

    vf = timestamp_filter(opts["timestamp_position"], h) if opts["timestamp"] else None
    out_fps = mode["fps"]
    if reencode:
        log("info", f"Bild wird neu berechnet ({reason}) – messe die Leistung bei {w}x{h} …")
        speed = measure_reencode(w, h, vf)
        if speed and speed < mode["fps"] * 0.95:
            cam_fps, out_fps = plan_fps(speed, mode)
            mode = dict(mode, fps=cam_fps)
            log("warning", f"Dieses Gerät schafft bei {w}x{h} mit Zeitstempel ca. {speed:.0f} Bilder/s – "
                           f"Stream läuft mit {out_fps:g} Bildern/s. Mehr Bilder/s: Zeitstempel aus "
                           "oder kleinere Auflösung.")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", *input_args(cam["path"], mode)]
    if reencode:
        filters = ([f"fps={out_fps:g}"] if out_fps + 0.5 < mode["fps"] else []) + ([vf] if vf else [])
        cmd += (["-vf", ",".join(filters)] if filters else []) + ["-c:v", "mjpeg", "-q:v", "5", "-threads", "0"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-an", "-f", "mjpeg", "-"]

    log("info", f"Kamera: {cam['card'] or cam['path']} ({cam['path']})")
    log("info", f"Bild:   {mode['fourcc']} {w}x{h} @ {out_fps:g} Bilder/s, "
                + ("neu berechnet" if reencode else "direkt von der Kamera (keine Last)")
                + (", mit Datum/Uhrzeit" if vf else ""))
    return "exec:" + " ".join(f'"{a}"' if " " in a else a for a in cmd) + "#killsignal=15#killtimeout=5"


def camera_busy(device: str) -> bool:
    rc, out = run(["v4l2-ctl", "-d", device, "--stream-mmap", "--stream-count=1", "--stream-to=/dev/null"],
                  timeout=15)
    return rc != 0 and "busy" in out.lower()


# --------------------------------------------------------------------------- #
# Home Assistant
# --------------------------------------------------------------------------- #
def supervisor_request(method: str, path: str, body: dict | None = None, timeout: float = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SUPERVISOR_URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"{method} {path} -> HTTP {err.code}: {err.read().decode(errors='replace')[:300]}") \
            from None


def ha_api(method: str, path: str, body: dict | None = None):
    return supervisor_request(method, "/core/api" + path, body)


def own_address() -> str:
    """Interne IP der App (Hostnamen kann Home Assistant nicht immer auflösen)."""
    if TOKEN:
        try:
            ip = (supervisor_request("GET", "/addons/self/info", timeout=15).get("data") or {}).get("ip_address")
            if ip and ip not in ("0.0.0.0", "127.0.0.1"):
                return ip
        except Exception as err:  # noqa: BLE001
            log("warning", f"IP der App nicht ermittelbar: {err}")
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return socket.gethostname()


def cleanup_old_options() -> None:
    """Optionen früherer Versionen entfernen (sonst Warnungen im Supervisor-Log)."""
    try:
        stored = dict((supervisor_request("GET", "/addons/self/info", timeout=15).get("data") or {})
                      .get("options") or {})
        clean = {k: v for k, v in stored.items() if k in OPTION_KEYS}
        if clean != stored:
            supervisor_request("POST", "/addons/self/options", {"options": clean}, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def fill_form(schema: list, values: dict) -> dict:
    out: dict = {}
    for field in schema or []:
        name = field.get("name")
        if not name:
            continue
        if field.get("type") == "expandable":
            out[name] = fill_form(field.get("schema", []), values)
        elif name in values:
            out[name] = values[name]
        elif (field.get("description") or {}).get("suggested_value") is not None:
            out[name] = field["description"]["suggested_value"]
        elif "default" in field:
            out[name] = field["default"]
    return out


def run_flow(start: str, body: dict, step: str, values: dict) -> dict:
    flow = ha_api("POST", start, body)
    for _ in range(6):
        if flow.get("type") != "form":
            return flow
        if flow.get("errors"):
            try:
                ha_api("DELETE", step + flow["flow_id"])
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"Home Assistant meldet: {flow['errors']}")
        flow = ha_api("POST", step + flow["flow_id"], fill_form(flow.get("data_schema", []), values))
    raise RuntimeError("Einrichtung hängt")


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def ws_rename(entry_id: str, title: str) -> None:
    """Entität auf camera.<name> umbenennen (z. B. camera.usb_webcam)."""
    try:
        import websocket  # py3-websocket-client
    except ImportError:
        return
    ws = websocket.create_connection(SUPERVISOR_URL.replace("http", "ws", 1) + "/core/websocket", timeout=30)
    try:
        json.loads(ws.recv())
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            return
        n = [0]

        def call(payload: dict) -> dict:
            n[0] += 1
            payload["id"] = n[0]
            ws.send(json.dumps(payload))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == n[0] and msg.get("type") == "result":
                    return msg

        entity_id = None
        for _ in range(15):
            for ent in call({"type": "config/entity_registry/list"}).get("result") or []:
                if ent.get("config_entry_id") == entry_id and ent.get("entity_id", "").startswith("camera."):
                    entity_id = ent["entity_id"]
            if entity_id:
                break
            time.sleep(2)
        if not entity_id:
            return
        wanted = "camera." + slugify(title)
        payload = {"type": "config/entity_registry/update", "entity_id": entity_id, "name": title}
        if wanted != entity_id:
            payload["new_entity_id"] = wanted
        if not call(payload).get("success") and "new_entity_id" in payload:
            payload.pop("new_entity_id")
            call(payload)
            wanted = entity_id
        log("info", f"Kamera-Entität: {wanted}")
    finally:
        ws.close()


def register_camera(opts: dict, mjpeg_url: str, still_url: str) -> None:
    """Kamera als MJPEG IP Camera anlegen bzw. aktualisieren; Einträge früherer Versionen ersetzen."""
    values = {"name": opts["camera_name"], "mjpeg_url": mjpeg_url, "still_image_url": still_url,
              "username": "", "password": "", "verify_ssl": False}
    urls = {"mjpeg_url": mjpeg_url, "still_image_url": still_url}
    state = load_state()
    entries = ha_api("GET", "/config/config_entries/entry") or []
    mine = state.get("entry_id")
    current = next((e for e in entries if e.get("entry_id") == mine), None)

    if current and current.get("domain") == "mjpeg" and state.get("version") == 2:
        if state.get("urls") != urls:
            log("info", "Adresse hat sich geändert – aktualisiere die Kamera in Home Assistant")
            run_flow("/config/config_entries/options/flow", {"handler": mine},
                     "/config/config_entries/options/flow/", values)
            state["urls"] = urls
            save_state(state)
        log("info", "Kamera ist in Home Assistant eingerichtet")
        return

    # Einträge früherer Versionen dieser App entfernen (gleiche ID oder gleicher Name)
    for entry in entries:
        if entry.get("domain") in ("generic", "mjpeg") and (
                entry.get("entry_id") == mine or entry.get("title") == opts["camera_name"]):
            log("info", f"Entferne alten Kamera-Eintrag '{entry.get('title')}' ({entry.get('domain')})")
            ha_api("DELETE", f"/config/config_entries/entry/{entry['entry_id']}")
            time.sleep(2)

    log("info", "Lege Kamera in Home Assistant an …")
    before = {e.get("entry_id") for e in ha_api("GET", "/config/config_entries/entry?domain=mjpeg") or []}
    result = run_flow("/config/config_entries/flow", {"handler": "mjpeg"}, "/config/config_entries/flow/", values)
    if result.get("type") == "abort" and result.get("reason") == "already_configured":
        log("info", "Kamera ist in Home Assistant bereits vorhanden")
        return
    if result.get("type") != "create_entry":
        raise RuntimeError(f"Einrichtung abgebrochen: {result.get('reason') or result}")
    new_id = (result.get("result") or {}).get("entry_id") if isinstance(result.get("result"), dict) else None
    if not new_id:
        fresh = [e for e in ha_api("GET", "/config/config_entries/entry?domain=mjpeg") or []
                 if e.get("entry_id") not in before]
        new_id = fresh[0]["entry_id"] if fresh else None
    save_state({"version": 2, "entry_id": new_id, "urls": urls})
    log("info", "Kamera in Home Assistant angelegt")
    if new_id:
        try:
            ws_rename(new_id, opts["camera_name"])
        except Exception as err:  # noqa: BLE001
            log("warning", f"Umbenennen nicht möglich: {err}")


def registration_worker(opts: dict, mjpeg_url: str, still_url: str) -> None:
    # warten, bis go2rtc das erste Bild liefert (HA prüft die Adresse beim Anlegen)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/frame.jpeg?src={STREAM}", timeout=10):
                break
        except Exception:  # noqa: BLE001
            time.sleep(2)
    for attempt in range(1, 6):
        try:
            register_camera(opts, mjpeg_url, still_url)
            return
        except Exception as err:  # noqa: BLE001
            log("warning", f"Einrichtung in Home Assistant fehlgeschlagen (Versuch {attempt}/5): {err}")
            time.sleep(15 * attempt)


# --------------------------------------------------------------------------- #
def main() -> int:
    opts = load_options()
    if TOKEN:
        threading.Thread(target=cleanup_old_options, daemon=True).start()
    if not opts["device"]:
        log("error", "Bitte im Tab 'Konfiguration' bei 'Kamera' deine Webcam auswählen.")
        return 1

    cam = find_camera(opts["device"])
    if not cam:
        return 1
    run(["v4l2-ctl", "-d", cam["path"], "-c", "power_line_frequency=1"], timeout=10)  # 50 Hz gegen Flimmern
    if camera_busy(cam["path"]):
        log("error", "Die Kamera wird gerade von einem anderen Programm benutzt (z. B. MotionEye). "
                     "Eine USB-Kamera kann nur EIN Programm gleichzeitig nutzen – dort stoppen und "
                     "diese App neu starten.")
        return 1
    try:
        source = build_source(opts, cam)
    except RuntimeError as err:
        log("error", str(err))
        return 1

    config = {
        "log": {"level": "warn", "format": "text"},
        "api": {"listen": f":{API_PORT}"},
        "rtsp": {"listen": ""},
        "webrtc": {"listen": ""},
        "streams": {STREAM: [source]},
        "preload": {STREAM: None},  # Kamera läuft dauerhaft -> sofort Bild, wie bei MotionEye
    }
    with open(GO2RTC_CONFIG, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)

    host = own_address()
    mjpeg_url = f"http://{host}:{API_PORT}/api/stream.mjpeg?src={STREAM}"
    still_url = f"http://{host}:{API_PORT}/api/frame.jpeg?src={STREAM}"
    log("info", f"Stream:    {mjpeg_url}")
    log("info", f"Standbild: {still_url}")

    proc = subprocess.Popen([GO2RTC_BIN, "-config", GO2RTC_CONFIG])

    def stop(_signum, _frame):
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if TOKEN:
        threading.Thread(target=registration_worker, args=(opts, mjpeg_url, still_url), daemon=True).start()
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
