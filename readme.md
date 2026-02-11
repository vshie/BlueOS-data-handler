# BlueOS Data Handler

Reads serial data from USB devices and forwards parsed values to **Mavlink2Rest** (NAMED_VALUE_FLOAT) and/or **Cockpit** WebSocket. Optionally logs to CSV.

---

## Install

Docker repo: `vshie/blueos-data-handler`  
Tag: `main`

Settings:

```json
{
  "ExposedPorts": {
    "8666/tcp": {},
    "8765/tcp": {}
  },
  "HostConfig": {
    "CpuPeriod": 100000,
    "CpuQuota": 100000,
    "Binds": [
      "/usr/blueos/extensions/data-handler:/app/logs",
      "/dev:/dev"
    ],
    "ExtraHosts": ["host.docker.internal:host-gateway"],
    "PortBindings": {
      "8666/tcp": [
        {
          "HostPort": "8666"
        }
      ],
      "8765/tcp": [
        {
          "HostPort": "8765"
        }
      ]
    },
    "NetworkMode": "host",
    "Privileged": true
  }
}
```

---

## Usage

### Web UI

- **URL:** Open the extension from the BlueOS extensions page, or go to port **8666** on the host (e.g. `http://blueos.local:8666`).
- **Port 8765** is used for the **Cockpit WebSocket**; Cockpit connects to `ws://<host>:8765` to receive `variable_name=value` updates.

### Tabs

1. **Data & Fields**  
   - Live value tiles for each configured variable.  
   - Incoming serial stream (console) with optional auto-scroll.  
   - **Field configuration:** set **Field Separator** (e.g. `,`) and **Name/Value Sep** (blank = positional columns; or e.g. `=` for `name=value`). Use **Detect** to guess separators from recent data.  
   - **Variable names** are used for Mavlink2Rest, Cockpit WS, and CSV headers (max 10 characters for MAVLink).  
   - Per-field toggles: **Send to MAVLink** and **Send to Cockpit**.  
   - **Raw mode:** disable parsing; raw lines are still sent to Cockpit WS and shown in the console.

2. **Connection**  
   - Choose **Serial port** (e.g. `/dev/ttyUSB0`, `/dev/ttyACM0`) and **Baud rate**.  
   - **Connect** / **Disconnect**. Connection intent is saved and restored on restart.  
   - Optional **Line delimiter** (default `\n`) and **Poll interval**.

3. **Logs**  
   - **Download** the CSV log (`data_log.csv`) or **Delete** logs. Logs are written under the bind-mounted path `/app/logs` (e.g. `/usr/blueos/extensions/data-handler` on the host).

### Outputs

- **Mavlink2Rest:** Parsed numeric values are sent as `NAMED_VALUE_FLOAT` to Mavlink2Rest (tries `host.docker.internal:6040`, `localhost`, etc.).  
- **Cockpit:** WebSocket server on port **8765**; Cockpit subscribes to receive `variable_name=value` (or raw lines in raw mode).  
- **CSV:** Rows appended to `data_log.csv` in the logs directory (with rotation when the file exceeds the size limit).

### API (optional)

- `GET /api/serial/ports` — list serial ports  
- `POST /api/serial/connect` — connect (body: `{"port": "/dev/ttyUSB0", "baud_rate": 9600}`)  
- `POST /api/serial/disconnect` — disconnect  
- `GET /api/config` — full config  
- `POST /api/config` — update config (partial JSON merged into state)  
- `GET /api/detect_separator` — suggest separators from recent buffer  
- `GET /api/data?limit=200` — recent parsed data  
- `GET /api/status` — connection and buffer status  
- `GET /api/logs` — download CSV log  
- `POST /api/logs/delete` — delete log file(s)
