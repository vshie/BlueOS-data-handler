#!/usr/bin/env python3
"""
BlueOS Data Handler Extension
Reads serial data from USB devices and forwards to Mavlink2Rest and/or Cockpit WebSocket.

Each configured field has a single "variable_name" (max 10 chars) that is used
identically for:
  - Mavlink2Rest NAMED_VALUE_FLOAT
  - Cockpit WebSocket data-lake (variable_name=value)
  - CSV log column header

Serial handling modelled on the NMEA-handler extension: explicit connect /
disconnect, background reader thread with start / stop, full state persistence
with auto-restore on boot.
"""
import asyncio
import csv
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from threading import Lock

import requests
import serial
import serial.tools.list_ports
import websockets
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from websockets.exceptions import ConnectionClosed

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_dir = Path("/app/logs")
log_dir.mkdir(parents=True, exist_ok=True)

app_logger = logging.getLogger("app")
_fh = logging.FileHandler(log_dir / "data_handler.log", mode="a")
_fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
app_logger.addHandler(_fh)
app_logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static")
CORS(app)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_VARIABLE_NAME_LEN = 10          # MAVLink NAMED_VALUE_FLOAT limit
LOG_FILE = log_dir / "data_log.csv"
MAX_CSV_SIZE_MB = 10
STATE_FILE = log_dir / "state.json"
WEBSOCKET_PORT = 8765

MAVLINK_ENDPOINTS = [
    "http://host.docker.internal:6040/v1/mavlink",
    "http://localhost:6040/v1/mavlink",
    "http://127.0.0.1:6040/v1/mavlink",
    "http://192.168.2.2:6040/v1/mavlink",
    "http://blueos.local:6040/v1/mavlink",
]

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
data_buffer = []
DATA_LOCK = Lock()


def _field_defaults():
    return {
        "index": 0,              # Column position (positional mode)
        "source_key": "",        # Label from incoming data (key_value mode)
        "variable_name": "",     # Output name for MAV + WS + CSV (max 10 chars)
        "type": "number",        # "number" or "string"
        "send_mavlink": True,
        "send_cockpit": True,
    }


def _normalise_field(f):
    d = _field_defaults()
    d.update(f)
    # Enforce 10-char limit
    d["variable_name"] = d["variable_name"][:MAX_VARIABLE_NAME_LEN]
    return d


# The "state" dict holds everything that persists across restarts:
# serial config, field definitions, output toggles, connection intent.
state = {
    "serial_port": None,
    "baud_rate": 9600,
    "is_connected": False,          # Whether we *intend* to be connected
    "line_delimiter": "\\n",
    "field_separator": ",",         # Separates chunks in the line
    "kv_separator": "",             # Separates name from value WITHIN a chunk.
                                    #   "" → positional mode (use column index)
                                    #   same as field_separator → alternating pairs
                                    #   different → standard key=value split
    "fields": [],
    "send_mavlink": True,
    "send_cockpit": True,
    "raw_mode": False,
}


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state():
    global state
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)

            # Migrate old config format
            if "field_names" in saved and "fields" not in saved:
                names = saved.pop("field_names", [])
                types = saved.pop("field_types", [])
                saved.pop("mavlink_name_prefix", None)
                fields = []
                for i, name in enumerate(names):
                    ftype = types[i] if i < len(types) else "number"
                    fields.append({
                        "index": i,
                        "variable_name": name[:MAX_VARIABLE_NAME_LEN],
                        "type": ftype,
                        "send_mavlink": True,
                        "send_cockpit": True,
                    })
                saved["fields"] = fields

            # Strip removed keys from earlier versions
            saved.pop("parse_mode", None)
            saved.pop("poll_interval", None)

            for k, v in saved.items():
                if k in state:
                    state[k] = v

            state["fields"] = [_normalise_field(f) for f in state["fields"]]
            app_logger.info(
                f"State loaded: port={state['serial_port']}, baud={state['baud_rate']}, "
                f"connected={state['is_connected']}, fields={len(state['fields'])}"
            )
        else:
            app_logger.info("No saved state, using defaults")
    except Exception as e:
        app_logger.error(f"Error loading state: {e}")


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        app_logger.info("State saved")
    except Exception as e:
        app_logger.error(f"Error saving state: {e}")


# ---------------------------------------------------------------------------
# USB port grid mapping (NMEA-handler style)
# Maps Linux USB location (e.g. "1-1.2") to grid cell id (e.g. "2.0 #2")
# Pi 4: 1-1.x = USB 2.0, 2-1.x = USB 3.0
# ---------------------------------------------------------------------------

def _location_to_usb_port(location):
    """Return {bus, hub_info} for grid highlighting. bus = grid cell id (2x2 USB + ETH)."""
    if not location:
        return None
    # Pi 4: 2x2 layout - 2.0 #1, 2.0 #2 (top), 3.0 #1, 3.0 #2 (bottom), ETH
    parts = [p for p in location.replace("-", ".").split(".") if p]
    try:
        if len(parts) >= 3:
            bus_num = int(parts[0])
            port_num = int(parts[-1])
            if bus_num == 1:
                if port_num <= 2:
                    return {"bus": f"2.0 #{port_num}", "hub_info": location}
                return {"bus": f"3.0 #{min(port_num - 2, 2)}", "hub_info": location}
            elif bus_num == 2:
                return {"bus": f"3.0 #{min(port_num, 2)}", "hub_info": location}
        elif len(parts) >= 2:
            return {"bus": f"2.0 #{min(int(parts[-1]), 2)}", "hub_info": location}
    except (ValueError, IndexError):
        pass
    return {"bus": location, "hub_info": location}


# ---------------------------------------------------------------------------
# Serial handler (modelled on NMEA-handler)
# ---------------------------------------------------------------------------

class SerialHandler:
    def __init__(self):
        self.serial_connection = None
        self.reader_thread = None
        self.should_stop = False
        self.lock = Lock()

    # -- Port discovery --

    def get_ports(self):
        """List available serial ports with physical location, using pyserial + by-id.
        Same approach as NMEA-handler: pyserial gives location/hwid, by-id gives stable names."""
        ports = []
        seen = set()

        # Primary: pyserial list_ports (includes location, description, hwid)
        # Only include ttyUSB* (USB-serial adapters); exclude serial*, ttyACM*, ttyAMA*
        try:
            for port_info in serial.tools.list_ports.comports(include_links=True):
                path = port_info.device
                if path in seen:
                    continue
                if "ttyUSB" not in path:
                    continue  # Only USB-serial (ttyUSB*); exclude serial*, ttyACM*, ttyAMA*
                seen.add(path)

                # Physical location (USB port hierarchy, e.g. "1-1.2")
                location = getattr(port_info, "location", None) or ""
                if not location:
                    continue  # Only list devices that are actually connected (have USB path)

                # Human-readable name: prefer description, else by-id basename, else device name
                name = port_info.description or os.path.basename(path)
                if not name or name == os.path.basename(path):
                    # Try by-id for a more descriptive name
                    by_id = Path("/dev/serial/by-id")
                    if by_id.exists():
                        for link in by_id.iterdir():
                            try:
                                if str(link.resolve()) == path:
                                    name = link.name
                                    break
                            except OSError:
                                pass

                # Human-readable location label (e.g. "USB 1-1.2")
                location_display = f"USB {location}" if location else ""

                # usb_port for grid highlighting (bus = grid cell id, hub_info = raw location)
                usb_port = _location_to_usb_port(location)

                ports.append({
                    "path": path,
                    "name": name,
                    "device": os.path.basename(path),
                    "display_name": name,
                    "location": location,
                    "location_display": location_display,
                    "usb_port": usb_port,
                })
        except Exception as e:
            app_logger.error(f"pyserial list_ports error: {e}")

        # Fallback: by-id if pyserial returned nothing (only ttyUSB*)
        if not ports:
            by_id = Path("/dev/serial/by-id")
            if by_id.exists():
                for link in by_id.iterdir():
                    try:
                        resolved = str(link.resolve())
                        if resolved not in seen and "ttyUSB" in resolved:
                            ports.append({
                                "path": resolved,
                                "name": link.name,
                                "device": os.path.basename(resolved),
                                "display_name": link.name,
                                "location": "",
                                "location_display": "",
                                "usb_port": None,
                            })
                            seen.add(resolved)
                    except OSError:
                        pass

        ports.sort(key=lambda x: x["path"])
        return ports

    # -- Connect / disconnect --

    def connect(self, port, baud_rate):
        """Explicitly connect to a serial port."""
        with self.lock:
            # Close existing
            if self.serial_connection and self.serial_connection.is_open:
                try:
                    self.serial_connection.close()
                except Exception:
                    pass

            if not Path(port).exists():
                return False, f"Port {port} does not exist"

            try:
                self.serial_connection = serial.Serial(
                    port=port,
                    baudrate=int(baud_rate),
                    timeout=1,
                )
                app_logger.info(f"Connected to {port} @ {baud_rate}")
            except serial.SerialException as e:
                self.serial_connection = None
                app_logger.error(f"Serial open error: {e}")
                return False, str(e)

        # Update & save state
        state["serial_port"] = port
        state["baud_rate"] = int(baud_rate)
        state["is_connected"] = True
        save_state()

        # Start reader
        self.start_reader()
        return True, f"Connected to {port} at {baud_rate} baud"

    def disconnect(self):
        """Explicitly disconnect."""
        self.stop_reader()
        with self.lock:
            if self.serial_connection and self.serial_connection.is_open:
                try:
                    self.serial_connection.close()
                except Exception:
                    pass
                self.serial_connection = None

        state["is_connected"] = False
        save_state()
        app_logger.info("Disconnected")
        return True, "Disconnected"

    @property
    def is_open(self):
        return self.serial_connection is not None and self.serial_connection.is_open

    # -- Reader thread --

    def start_reader(self):
        if self.reader_thread and self.reader_thread.is_alive():
            return
        self.should_stop = False
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()
        app_logger.info("Reader thread started")

    def stop_reader(self):
        self.should_stop = True
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=2.0)
        app_logger.info("Reader thread stopped")

    def _read_loop(self):
        """Background loop: read serial data, parse, dispatch."""
        while not self.should_stop:
            if not self.is_open:
                time.sleep(1)
                continue

            try:
                raw = b""
                with self.lock:
                    if self.serial_connection and self.serial_connection.is_open:
                        waiting = self.serial_connection.in_waiting
                        if waiting > 0:
                            raw = self.serial_connection.read(waiting)
                        else:
                            raw = self.serial_connection.readline()

                if raw:
                    text = raw.decode("utf-8", errors="replace").strip()
                    if text:
                        delim = state["line_delimiter"].encode().decode("unicode_escape")
                        lines = [l.strip() for l in text.split(delim) if l.strip()]
                        for line in lines:
                            self._process_line(line)

            except Exception as e:
                app_logger.error(f"Read error: {e}")
                time.sleep(1)
                continue

            # Process data immediately when it arrives; only sleep when idle to avoid busy-loop
            if not raw:
                time.sleep(0.01)

    def _process_line(self, line):
        """Parse a single line and dispatch to outputs."""
        app_logger.info(f"Serial: {line}")
        measurement = self._parse_line(line)

        # Buffer
        with DATA_LOCK:
            data_buffer.append(measurement)
            if len(data_buffer) > 500:
                del data_buffer[:-500]

        # CSV
        _write_csv(measurement)

        # Dispatch to outputs
        fields = state["fields"]
        for field in fields:
            vname = field.get("variable_name", "")
            if not vname:
                continue
            val = measurement.get(vname)
            if val is None:
                continue

            # Mavlink
            if (state["send_mavlink"]
                    and field.get("send_mavlink", True)
                    and field.get("type") == "number"):
                try:
                    _send_named_value_float(vname, float(val))
                except (ValueError, TypeError):
                    pass

            # Cockpit WS
            if state["send_cockpit"] and field.get("send_cockpit", True):
                ws_latest_values[vname] = val

        # Raw fallback for Cockpit
        if state["send_cockpit"] and (state["raw_mode"] or not fields):
            ws_latest_values["serial-raw"] = line

    @staticmethod
    def _coerce(raw_val, ftype):
        """Convert raw_val to the right type."""
        if ftype == "number":
            try:
                return float(raw_val)
            except ValueError:
                return raw_val
        return raw_val

    @staticmethod
    def _parse_line(raw_line):
        """Parse a serial line according to the configured separators.

        Three modes determined by kv_separator:
          1. kv_separator empty           → POSITIONAL: column index maps to field
          2. kv_separator == field_sep     → ALTERNATING PAIRS: name,value,name,value,...
          3. kv_separator != field_sep     → KEY=VALUE: name=value<sep>name=value<sep>...
        """
        measurement = {
            "timestamp": datetime.now().isoformat(),
            "raw_line": raw_line,
        }
        fields = state["fields"]
        if state["raw_mode"] or not fields:
            return measurement

        sep = state["field_separator"]
        kv_sep = state.get("kv_separator", "").strip()
        parts = [p.strip() for p in raw_line.split(sep)]

        if not kv_sep:
            # ── POSITIONAL ──
            for field in fields:
                idx = field.get("index", 0)
                vname = field.get("variable_name", "")
                ftype = field.get("type", "number")
                if not vname:
                    continue
                if idx < len(parts):
                    measurement[vname] = SerialHandler._coerce(parts[idx], ftype)
                else:
                    measurement[vname] = None

        elif kv_sep == sep:
            # ── ALTERNATING PAIRS ──  (name,value,name,value,...)
            # Build source_key → field lookup
            key_map = {}
            for field in fields:
                sk = field.get("source_key", "").strip()
                if sk:
                    key_map[sk] = field

            i = 0
            while i + 1 < len(parts):
                label = parts[i].strip()
                raw_val = parts[i + 1].strip()
                i += 2
                field = key_map.get(label)
                if not field:
                    continue
                vname = field.get("variable_name", "")
                if not vname:
                    continue
                measurement[vname] = SerialHandler._coerce(
                    raw_val, field.get("type", "number")
                )

        else:
            # ── KEY=VALUE ──  (name=value<sep>name=value<sep>...)
            key_map = {}
            for field in fields:
                sk = field.get("source_key", "").strip()
                if sk:
                    key_map[sk] = field

            for part in parts:
                if kv_sep in part:
                    label, _, raw_val = part.partition(kv_sep)
                    label = label.strip()
                    raw_val = raw_val.strip()
                    field = key_map.get(label)
                    if not field:
                        continue
                    vname = field.get("variable_name", "")
                    if not vname:
                        continue
                    measurement[vname] = SerialHandler._coerce(
                        raw_val, field.get("type", "number")
                    )

        return measurement


serial_handler = SerialHandler()


# ---------------------------------------------------------------------------
# Mavlink2Rest
# ---------------------------------------------------------------------------

def _send_named_value_float(name: str, value: float):
    name_10 = name[:MAX_VARIABLE_NAME_LEN]
    name_array = list(name_10.ljust(10, "\x00"))
    payload = {
        "header": {"system_id": 255, "component_id": 0, "sequence": 0},
        "message": {
            "type": "NAMED_VALUE_FLOAT",
            "time_boot_ms": 0,
            "value": value,
            "name": name_array,
        },
    }
    for endpoint in MAVLINK_ENDPOINTS:
        try:
            resp = requests.post(endpoint, json=payload, timeout=2.0)
            if resp.status_code == 200:
                app_logger.info(f"MAV OK: {name_10}={value} via {endpoint}")
                return True
        except Exception:
            continue
    app_logger.warning(f"MAV FAIL: {name_10}={value}")
    return False


# ---------------------------------------------------------------------------
# Cockpit WebSocket
# ---------------------------------------------------------------------------

ws_clients = set()
ws_latest_values = {}


async def _ws_handler(websocket):
    ws_clients.add(websocket)
    app_logger.info(f"WS client connected: {websocket.remote_address}")
    try:
        for k, v in ws_latest_values.items():
            await websocket.send(f"{k}={v}")
        while True:
            await asyncio.sleep(0.1)
            for k, v in ws_latest_values.items():
                try:
                    await websocket.send(f"{k}={v}")
                except ConnectionClosed:
                    break
    except ConnectionClosed:
        pass
    finally:
        ws_clients.discard(websocket)
        app_logger.info("WS client disconnected")


def _start_ws_server():
    loop = asyncio.new_event_loop()

    async def _serve():
        async with websockets.serve(_ws_handler, "0.0.0.0", WEBSOCKET_PORT):
            app_logger.info(f"WS server on ws://0.0.0.0:{WEBSOCKET_PORT}")
            await asyncio.Future()

    loop.run_until_complete(_serve())


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------

def _csv_headers():
    names = [f["variable_name"] for f in state["fields"] if f.get("variable_name")]
    if names:
        return ["timestamp"] + names + ["raw_line"]
    return ["timestamp", "raw_line"]


def _write_csv(measurement):
    try:
        log_path = str(LOG_FILE)
        file_exists = os.path.exists(log_path)

        if file_exists:
            size_mb = os.path.getsize(log_path) / (1024 * 1024)
            if size_mb >= MAX_CSV_SIZE_MB:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                os.rename(log_path, str(log_dir / f"data_log_backup_{ts}.csv"))
                file_exists = False

        headers = _csv_headers()
        with open(log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            writer.writerow(measurement)
            f.flush()
    except Exception as e:
        app_logger.error(f"CSV error: {e}")


# ---------------------------------------------------------------------------
# Separator auto-detection
# ---------------------------------------------------------------------------

CANDIDATE_SEPARATORS = [",", "\t", ";", "|", " ", ":"]
CANDIDATE_KV_SEPARATORS = ["=", ":"]


def detect_separator(lines):
    """Analyse serial lines and determine field_separator and kv_separator.

    Returns dict with:
      field_separator  – what splits the line into chunks
      kv_separator     – "" for positional, same as field_sep for alternating,
                         or a different char for standard key=value
      pattern          – "positional", "key_value", or "alternating"
      candidates       – per-separator scoring details
    """
    if not lines:
        return {"field_separator": ",", "kv_separator": "",
                "pattern": "positional", "candidates": {}}

    # ── Score each candidate field separator ──
    results = {}
    for sep in CANDIDATE_SEPARATORS:
        counts = [len(line.split(sep)) for line in lines]
        counter = Counter(counts)
        most_common_count, most_common_freq = counter.most_common(1)[0]
        consistency = most_common_freq / len(lines)
        results[sep] = {
            "separator": sep,
            "display": repr(sep),
            "field_count": most_common_count,
            "consistency": round(consistency, 2),
        }

    best = max(
        (r for r in results.values() if r["field_count"] > 1),
        key=lambda r: (r["consistency"], r["field_count"]),
        default=None,
    )
    chosen_sep = best["separator"] if best else ","

    # ── Check for key=value within each chunk ──
    kv_detected = None
    for kv_cand in CANDIDATE_KV_SEPARATORS:
        if kv_cand == chosen_sep:
            continue  # can't be the same; we'll check alternating below
        match_count = 0
        total_chunks = 0
        for line in lines:
            parts = [p.strip() for p in line.split(chosen_sep)]
            for part in parts:
                total_chunks += 1
                if kv_cand in part:
                    left, _, right = part.partition(kv_cand)
                    if left.strip() and right.strip():
                        match_count += 1
        if total_chunks > 0 and (match_count / total_chunks) >= 0.7:
            kv_detected = kv_cand
            break

    if kv_detected:
        return {"field_separator": chosen_sep, "kv_separator": kv_detected,
                "pattern": "key_value", "candidates": results}

    # ── Check for alternating pairs  (name,value,name,value,...) ──
    # Heuristic: even number of parts, and every other part is non-numeric
    # (looks like a label) while the next part IS numeric.
    alt_score = 0
    alt_total = 0
    for line in lines:
        parts = [p.strip() for p in line.split(chosen_sep)]
        if len(parts) >= 4 and len(parts) % 2 == 0:
            alt_total += 1
            pair_ok = 0
            for i in range(0, len(parts) - 1, 2):
                label = parts[i]
                value = parts[i + 1]
                # Label should NOT be a plain number; value should be
                label_is_text = not _is_number(label) and label != ""
                value_is_num = _is_number(value)
                if label_is_text and value_is_num:
                    pair_ok += 1
            if pair_ok == len(parts) // 2:
                alt_score += 1

    if alt_total > 0 and (alt_score / alt_total) >= 0.7:
        return {"field_separator": chosen_sep, "kv_separator": chosen_sep,
                "pattern": "alternating", "candidates": results}

    # ── Fallback: positional ──
    return {"field_separator": chosen_sep, "kv_separator": "",
            "pattern": "positional", "candidates": results}


def _is_number(s):
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Flask API
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)


# -- Serial --

@app.route("/api/serial/ports", methods=["GET"])
def api_ports():
    return jsonify({"ports": serial_handler.get_ports()})


@app.route("/api/serial/connect", methods=["POST"])
def api_connect():
    data = request.json or {}
    port = data.get("port")
    baud = data.get("baud_rate", state["baud_rate"])
    if not port:
        return jsonify({"success": False, "message": "No port specified"}), 400
    ok, msg = serial_handler.connect(port, baud)
    return jsonify({"success": ok, "message": msg})


@app.route("/api/serial/disconnect", methods=["POST"])
def api_disconnect():
    ok, msg = serial_handler.disconnect()
    return jsonify({"success": ok, "message": msg})


# -- Config --

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(state)


@app.route("/api/config", methods=["POST"])
def api_update_config():
    new = request.json
    if not new:
        return jsonify({"success": False, "message": "No data"}), 400

    if "baud_rate" in new:
        try:
            new["baud_rate"] = int(new["baud_rate"])
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "Invalid baud rate"}), 400

    if "fields" in new:
        new["fields"] = [_normalise_field(f) for f in new["fields"]]

    # Normalise kv_separator: null/None → empty string
    if "kv_separator" in new and new["kv_separator"] is None:
        new["kv_separator"] = ""

    # Strip removed/legacy keys
    new.pop("poll_interval", None)

    # Merge (but don't let the frontend set is_connected)
    for k, v in new.items():
        if k in state and k != "is_connected":
            state[k] = v

    save_state()

    # If baud or port changed and we're connected, reconnect
    if state["is_connected"] and ("serial_port" in new or "baud_rate" in new):
        serial_handler.connect(state["serial_port"], state["baud_rate"])

    return jsonify({"success": True, "config": state})


# -- Separator detection --

@app.route("/api/detect_separator", methods=["GET"])
def api_detect_separator():
    """Analyse recent serial lines and suggest separators + detect format."""
    with DATA_LOCK:
        lines = [m.get("raw_line", "") for m in data_buffer[-20:] if m.get("raw_line")]
    if not lines:
        return jsonify({"success": False, "message": "No data in buffer yet"})
    result = detect_separator(lines)
    return jsonify({
        "success": True,
        "field_separator": result["field_separator"],
        "field_separator_display": repr(result["field_separator"]),
        "kv_separator": result["kv_separator"],
        "pattern": result["pattern"],
        "candidates": result["candidates"],
    })


# -- Live data --

@app.route("/api/data", methods=["GET"])
def api_data():
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    with DATA_LOCK:
        out = list(data_buffer[-limit:])
    return jsonify(out)


# -- Status --

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "serial_connected": serial_handler.is_open,
        "serial_port": state["serial_port"],
        "baud_rate": state["baud_rate"],
        "ws_clients": len(ws_clients),
        "buffer_size": len(data_buffer),
        "send_mavlink": state["send_mavlink"],
        "send_cockpit": state["send_cockpit"],
        "fields_count": len(state["fields"]),
    })


# -- Logs --

@app.route("/api/logs", methods=["GET"])
def api_download_logs():
    p = str(LOG_FILE)
    if os.path.exists(p):
        return send_file(p, mimetype="text/csv", as_attachment=True,
                         download_name="data_handler_log.csv")
    return "No log file found", 404


@app.route("/api/logs/delete", methods=["POST"])
def api_delete_logs():
    try:
        deleted = 0
        p = str(LOG_FILE)
        if os.path.exists(p):
            os.remove(p)
            deleted += 1
        for f in Path(log_dir).glob("data_log_backup_*.csv"):
            f.unlink()
            deleted += 1
        return jsonify({"success": True, "message": f"Deleted {deleted} file(s)"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# -- Service registration --

@app.route("/register_service")
def register_service():
    return send_from_directory("static", "register_service")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    load_state()

    # Restore serial connection if it was active
    if state["is_connected"] and state["serial_port"]:
        app_logger.info(
            f"Restoring connection to {state['serial_port']} @ {state['baud_rate']}"
        )
        ok, msg = serial_handler.connect(state["serial_port"], state["baud_rate"])
        if ok:
            app_logger.info("Connection restored")
        else:
            app_logger.error(f"Restore failed: {msg}")

    # WebSocket server thread
    ws_thread = threading.Thread(target=_start_ws_server, daemon=True)
    ws_thread.start()

    # Flask (use waitress in production, fall back to built-in for dev)
    # Retry binding in case the previous container hasn't fully released the port yet.
    MAX_RETRIES = 6
    RETRY_DELAY = 5  # seconds between retries

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            try:
                from waitress import serve as waitress_serve
                app_logger.info(f"Starting with waitress on port 8666 (attempt {attempt}/{MAX_RETRIES})")
                waitress_serve(app, host="0.0.0.0", port=8666)
            except ImportError:
                app_logger.info(f"waitress not available, using Flask dev server on port 8666 (attempt {attempt}/{MAX_RETRIES})")
                app.run(host="0.0.0.0", port=8666)
            break  # server exited normally
        except OSError as e:
            if e.errno == 98 and attempt < MAX_RETRIES:
                app_logger.warning(
                    f"Port 8080 already in use, retrying in {RETRY_DELAY}s "
                    f"({attempt}/{MAX_RETRIES})…"
                )
                time.sleep(RETRY_DELAY)
            else:
                raise
