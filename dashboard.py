#!/usr/bin/env python3
"""
Bosch eBike live dashboard.

Subscribes to the bridge's MQTT topics and serves a real-time web UI at
http://e-bike-bridge.local:8080  (or the Pi's IP on port 8080).

Updates are pushed to the browser via Server-Sent Events (SSE) — no polling,
no page reloads. Works in any modern browser, including phones.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import yaml
from flask import Flask, Response, render_template_string
import paho.mqtt.client as mqtt

# ── HTML template ─────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>eBike Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100vh;
      padding: 24px 16px;
    }

    header {
      text-align: center;
      margin-bottom: 32px;
    }
    header h1 { font-size: 1.6rem; font-weight: 700; letter-spacing: -0.5px; }
    header p  { color: #8b949e; font-size: 0.85rem; margin-top: 4px; }

    #bikes {
      display: flex;
      gap: 20px;
      flex-wrap: wrap;
      justify-content: center;
      max-width: 900px;
      margin: 0 auto;
    }

    .bike-card {
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 14px;
      padding: 24px;
      flex: 1;
      min-width: 280px;
      max-width: 420px;
    }

    .bike-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 20px;
    }
    .status-dot {
      width: 10px; height: 10px;
      border-radius: 50%;
      background: #f85149;
      flex-shrink: 0;
    }
    .status-dot.online { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
    .bike-name { font-size: 1.1rem; font-weight: 600; }
    .bike-status { font-size: 0.75rem; color: #8b949e; margin-left: auto; }

    .speed-block {
      text-align: center;
      padding: 16px 0 20px;
    }
    .speed-value {
      font-size: 5rem;
      font-weight: 800;
      line-height: 1;
      color: #58a6ff;
      font-variant-numeric: tabular-nums;
    }
    .speed-unit { font-size: 1.2rem; color: #8b949e; margin-top: 4px; }

    .battery-section { margin-bottom: 20px; }
    .battery-label {
      display: flex;
      justify-content: space-between;
      font-size: 0.8rem;
      color: #8b949e;
      margin-bottom: 6px;
    }
    .battery-track {
      background: #21262d;
      border-radius: 6px;
      height: 14px;
      overflow: hidden;
    }
    .battery-fill {
      height: 100%;
      border-radius: 6px;
      transition: width 0.6s ease, background 0.4s ease;
    }

    .metrics {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 20px;
    }
    .metric {
      background: #0d1117;
      border-radius: 10px;
      padding: 12px;
      text-align: center;
    }
    .metric-value {
      font-size: 1.6rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }
    .metric-label { font-size: 0.7rem; color: #8b949e; margin-top: 2px; text-transform: uppercase; letter-spacing: 0.5px; }

    .sensors {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .sensor {
      display: flex;
      align-items: center;
      gap: 5px;
      background: #21262d;
      border-radius: 20px;
      padding: 5px 12px;
      font-size: 0.78rem;
      color: #8b949e;
      transition: background 0.3s, color 0.3s;
    }
    .sensor.on  { background: #1f6feb33; color: #58a6ff; border: 1px solid #1f6feb55; }
    .sensor.warn { background: #d2992233; color: #d29922; border: 1px solid #d2992255; }

    .waiting {
      text-align: center;
      color: #8b949e;
      padding: 80px 0;
      width: 100%;
    }
    .waiting .icon { font-size: 3rem; margin-bottom: 16px; }
    .waiting p { font-size: 0.9rem; }
  </style>
</head>
<body>
  <header>
    <h1>🚲 eBike Dashboard</h1>
    <p>Live data via Bluetooth · updates in real time</p>
  </header>
  <div id="bikes">
    <div class="waiting">
      <div class="icon">📡</div>
      <p>Waiting for bikes to connect…</p>
    </div>
  </div>

  <script>
    const state = {};

    const src = new EventSource("/stream");
    src.onmessage = (e) => {
      const d = JSON.parse(e.data);
      state[d.slug] = d;
      render();
    };
    src.onerror = () => {
      document.querySelector("header p").textContent = "⚠ Connection to Pi lost — retrying…";
    };

    function batteryColor(pct) {
      if (pct >= 50) return "#3fb950";
      if (pct >= 20) return "#d29922";
      return "#f85149";
    }

    function fmt(val, decimals = 0) {
      if (val == null) return "–";
      return decimals > 0 ? val.toFixed(decimals) : Math.round(val).toString();
    }

    function chip(icon, label, state, warnWhenOn = false) {
      const cls = state ? (warnWhenOn ? "warn" : "on") : "";
      return `<div class="sensor ${cls}">${icon} ${label}</div>`;
    }

    function render() {
      const slugs = Object.keys(state);
      const el = document.getElementById("bikes");
      if (slugs.length === 0) return;

      el.innerHTML = slugs.map(slug => {
        const b = state[slug];
        const d = b.data || {};
        const online = b.available;
        const soc = d.battery_soc ?? 0;
        const color = batteryColor(soc);

        return `
        <div class="bike-card">
          <div class="bike-header">
            <div class="status-dot ${online ? 'online' : ''}"></div>
            <div class="bike-name">${b.name || slug}</div>
            <div class="bike-status">${online ? 'Connected' : 'Offline'}</div>
          </div>

          <div class="speed-block">
            <div class="speed-value">${fmt(d.speed_kmh, 1)}</div>
            <div class="speed-unit">km/h</div>
          </div>

          <div class="battery-section">
            <div class="battery-label">
              <span>Battery</span>
              <span style="color:${color};font-weight:600">${soc}%</span>
            </div>
            <div class="battery-track">
              <div class="battery-fill" style="width:${soc}%;background:${color}"></div>
            </div>
          </div>

          <div class="metrics">
            <div class="metric">
              <div class="metric-value">${fmt(d.cadence)}</div>
              <div class="metric-label">Cadence rpm</div>
            </div>
            <div class="metric">
              <div class="metric-value">${fmt(d.rider_power)}</div>
              <div class="metric-label">Power W</div>
            </div>
            <div class="metric">
              <div class="metric-value">${fmt(d.odometer_km, 1)}</div>
              <div class="metric-label">Odometer km</div>
            </div>
            <div class="metric">
              <div class="metric-value">${fmt(d.ambient_lux, 0)}</div>
              <div class="metric-label">Brightness lx</div>
            </div>
          </div>

          <div class="sensors">
            ${chip('💡', 'Light on',       d.bike_light)}
            ${chip('🏃', 'In motion',      d.in_motion)}
            ${chip('🔌', 'Charging',       d.charger_connected)}
            ${chip('🔒', 'Locked',         d.system_locked)}
            ${chip('🔋', 'Light reserve',  d.light_reserve,       true)}
            ${chip('🔧', 'Diagnosis',      d.diagnosis_active,    true)}
          </div>
        </div>`;
      }).join("");
    }
  </script>
</body>
</html>
"""

# ── App & state ───────────────────────────────────────────────────────────────

app = Flask(__name__)

_state: dict[str, dict] = {}   # slug → {name, available, data}
_lock = threading.Lock()
_subscribers: list[queue.Queue] = []


def _push(payload: str) -> None:
    """Push a JSON string to all connected SSE clients."""
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(payload)
        except queue.Full:
            dead.append(q)
    for q in dead:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


# ── MQTT ──────────────────────────────────────────────────────────────────────

def _start_mqtt(config: dict, name_map: dict[str, str]) -> None:
    """Subscribe to MQTT and populate _state."""
    mqtt_cfg = config["mqtt"]
    base     = mqtt_cfg.get("base_topic", "bosch_ebike").rstrip("/")

    client = mqtt.Client(client_id="bosch-ebike-dashboard", clean_session=False)
    if mqtt_cfg.get("username"):
        client.username_pw_set(mqtt_cfg["username"], mqtt_cfg.get("password"))

    def on_connect(c, *_):
        c.subscribe(f"{base}/+/state")
        c.subscribe(f"{base}/+/availability")

    def on_message(c, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) < 3:
            return
        slug = parts[-2]
        kind = parts[-1]

        with _lock:
            if slug not in _state:
                _state[slug] = {
                    "slug":      slug,
                    "name":      name_map.get(slug, slug.replace("_", ":")),
                    "available": False,
                    "data":      {},
                }
            if kind == "availability":
                _state[slug]["available"] = (msg.payload.decode() == "online")
            elif kind == "state":
                try:
                    _state[slug]["data"] = json.loads(msg.payload)
                except (json.JSONDecodeError, ValueError):
                    pass
            snap = dict(_state[slug])

        _push(json.dumps(snap))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(mqtt_cfg["broker"], int(mqtt_cfg.get("port", 1883)))
    client.loop_start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/stream")
def stream():
    """SSE endpoint — keeps connection open and pushes state updates."""
    def generate():
        q: queue.Queue = queue.Queue(maxsize=50)
        _subscribers.append(q)

        # Send current state of all known bikes immediately on connect
        with _lock:
            for bike in _state.values():
                yield f"data: {json.dumps(bike)}\n\n"

        try:
            while True:
                try:
                    payload = q.get(timeout=25)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"   # prevent proxy timeouts
        finally:
            try:
                _subscribers.remove(q)
            except ValueError:
                pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":  "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if present
        },
    )


@app.route("/api/state")
def api_state():
    """JSON snapshot of all bike states — useful for debugging."""
    with _lock:
        return json.dumps(list(_state.values())), 200, {"Content-Type": "application/json"}


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    config_path = "/etc/bosch-ebike-bridge/config.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    with open(config_path) as fh:
        config = yaml.safe_load(fh)

    # Build slug → friendly name map from config bikes list
    def _slug(addr: str) -> str:
        return addr.replace(":", "_").lower()

    name_map = {
        _slug(b["address"]): b.get("name", b["address"])
        for b in config.get("bikes", [])
    }

    _start_mqtt(config, name_map)

    port = config.get("dashboard_port", 8080)
    print(f"Dashboard running at http://e-bike-bridge.local:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
