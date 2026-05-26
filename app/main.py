"""Entry-point: avvia in parallelo Modbus TCP server (porta 502), OpenHAB
poller (background task) e admin UI FastAPI (porta 5050)."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app import registers as R
from app.config import Settings, get_settings
from app.openhab import OpenHabClient
from app.server import HuaweiModbusEmulator, run_modbus_server, run_openhab_poller

log = logging.getLogger("huawei-emu")


def _configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    if level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "pymodbus.logging", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    emulator = HuaweiModbusEmulator(settings)
    oh = OpenHabClient(settings.openhab_base_url, settings.openhab_request_timeout_s)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        log.info("starting Modbus server + OH poller in background")
        modbus_task = asyncio.create_task(
            run_modbus_server(emulator, settings.modbus_host, settings.modbus_port)
        )
        poller_task = asyncio.create_task(
            run_openhab_poller(emulator, oh, settings.poll_interval_s)
        )
        try:
            yield
        finally:
            log.info("shutdown: cancelling background tasks")
            for t in (modbus_task, poller_task):
                t.cancel()
            for t in (modbus_task, poller_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await oh.close()

    app = FastAPI(title="huawei-sun2000-emulator", version="0.1.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.emulator = emulator

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return INDEX_HTML

    @app.get("/healthz")
    async def healthz():
        age = time.time() - emulator.last_update_ts if emulator.last_update_ts else None
        return {
            "ok": True,
            "modbus_port": settings.modbus_port,
            "modbus_unit_id": settings.modbus_unit_id,
            "last_oh_update_ok": emulator.last_update_ok,
            "last_oh_update_age_s": round(age, 1) if age else None,
            "last_oh_error": emulator.last_error,
        }

    @app.get("/admin/config")
    async def admin_config():
        return settings.model_dump()

    @app.get("/admin/registers")
    async def admin_registers():
        """Dump decodificato dei registri principali (per debug)."""
        reg = emulator
        out: dict[str, Any] = {}

        def get_u16(addr): return R.decode_u16(reg.read_register(addr, 1))
        def get_i16(addr): return R.decode_i16(reg.read_register(addr, 1))
        def get_u32(addr): return R.decode_u32(reg.read_register(addr, 2))
        def get_i32(addr): return R.decode_i32(reg.read_register(addr, 2))
        def get_str(addr, n): return R.decode_string(reg.read_register(addr, n))

        out["identity"] = {
            "model": get_str(R.ADDR_MODEL_NAME, 15),
            "sn": get_str(R.ADDR_SN, 10),
            "pn": get_str(R.ADDR_PN, 10),
            "fw": get_str(R.ADDR_FIRMWARE_VERSION, 15),
            "pv_strings": get_u16(R.ADDR_NUMBER_OF_PV_STRINGS),
            "rated_power_W": get_u32(R.ADDR_RATED_POWER),
        }
        out["realtime"] = {
            "pv1_voltage_V": get_i16(R.ADDR_PV1_VOLTAGE) / 10,
            "pv2_voltage_V": get_i16(R.ADDR_PV2_VOLTAGE) / 10,
            "pv1_current_A": get_i16(R.ADDR_PV1_CURRENT) / 100,
            "pv2_current_A": get_i16(R.ADDR_PV2_CURRENT) / 100,
            "input_power_W": get_i32(R.ADDR_INPUT_POWER),
            "grid_voltage_A_V": get_u16(R.ADDR_GRID_VOLTAGE_A) / 10,
            "grid_voltage_B_V": get_u16(R.ADDR_GRID_VOLTAGE_B) / 10,
            "grid_voltage_C_V": get_u16(R.ADDR_GRID_VOLTAGE_C) / 10,
            "grid_current_A_A": get_i32(R.ADDR_GRID_CURRENT_A) / 1000,
            "grid_current_B_A": get_i32(R.ADDR_GRID_CURRENT_B) / 1000,
            "grid_current_C_A": get_i32(R.ADDR_GRID_CURRENT_C) / 1000,
            "active_power_W": get_i32(R.ADDR_ACTIVE_POWER),
            "power_factor": get_i16(R.ADDR_POWER_FACTOR) / 1000,
            "grid_frequency_Hz": get_u16(R.ADDR_GRID_FREQUENCY) / 100,
            "efficiency_pct": get_u16(R.ADDR_EFFICIENCY) / 100,
            "internal_temp_C": get_i16(R.ADDR_INTERNAL_TEMPERATURE) / 10,
            "device_status": f"0x{get_u16(R.ADDR_DEVICE_STATUS):04X}",
        }
        out["energy"] = {
            "daily_yield_kWh": get_u32(R.ADDR_DAILY_YIELD) / 100,
            "accumulated_yield_kWh": get_u32(R.ADDR_ACCUMULATED_YIELD) / 100,
        }
        if settings.huawei_has_battery:
            out["battery"] = {
                "running_status": get_u16(R.ADDR_BATTERY_RUNNING_STATUS),
                "soc_pct": get_u16(R.ADDR_BATTERY_SOC) / 10,
                "temperature_C": get_i16(R.ADDR_BATTERY_TEMPERATURE) / 10,
                "charge_discharge_power_W": get_i32(R.ADDR_BATTERY_CHARGE_DISCHARGE_POWER),
            }
        out["meter"] = {
            "active_power_W": get_i32(R.ADDR_METER_ACTIVE_POWER),
            "voltage_A_V": get_i32(R.ADDR_METER_GRID_VOLTAGE_A) / 10,
            "voltage_B_V": get_i32(R.ADDR_METER_GRID_VOLTAGE_B) / 10,
            "voltage_C_V": get_i32(R.ADDR_METER_GRID_VOLTAGE_C) / 10,
        }
        return out

    return app


INDEX_HTML = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Huawei SUN2000 Emulator</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 900px;
         margin: 2em auto; padding: 0 1em; background: #fafafa; color: #222; }
  h1 small { color: #888; font-size: .55em; font-weight: normal; }
  h2 { border-bottom: 1px solid #ddd; padding-bottom: .3em; }
  .panel { background: white; padding: 1em 1.2em; border-radius: 8px; margin: 1em 0;
           box-shadow: 0 1px 3px rgba(0,0,0,.05); }
  pre { background: #1e1e1e; color: #ddd; padding: 1em; border-radius: 5px;
        overflow-x: auto; font-size: .82em; }
  button { padding: .5em 1em; margin: .15em; border: 1px solid #ccc; border-radius: 5px;
           background: white; cursor: pointer; }
  .pill { display: inline-block; padding: .15em .65em; border-radius: 100px;
          font-size: .85em; font-weight: 600; }
  .pill.ok { background: #d4f5d4; color: #060; }
  .pill.err { background: #fcd; color: #900; }
</style>
</head>
<body>
<h1>Huawei SUN2000 Emulator <small>v0.1.0</small></h1>
<div class="panel">
  <h2>Stato</h2>
  <p>Modbus TCP: <strong id="modbus-info">caricamento...</strong></p>
  <p>OpenHAB poller: <span id="oh-pill" class="pill">?</span> <small id="oh-age" style="color:#777"></small></p>
</div>
<div class="panel">
  <h2>Registri live</h2>
  <button onclick="reload()">&#8635; Refresh</button>
  <pre id="regs">caricamento...</pre>
</div>
<script>
async function reload() {
  try {
    const h = await (await fetch('/healthz')).json();
    document.getElementById('modbus-info').textContent =
      'porta ' + h.modbus_port + ', unit ' + h.modbus_unit_id;
    const pill = document.getElementById('oh-pill');
    if (h.last_oh_update_ok) { pill.textContent = 'OK'; pill.className = 'pill ok'; }
    else                      { pill.textContent = 'KO'; pill.className = 'pill err'; }
    document.getElementById('oh-age').textContent =
      h.last_oh_update_age_s != null ? '(' + h.last_oh_update_age_s + 's fa)' : '';

    const r = await (await fetch('/admin/registers')).json();
    document.getElementById('regs').textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    document.getElementById('regs').textContent = 'errore: ' + e.message;
  }
}
reload();
setInterval(reload, 5000);
</script>
</body>
</html>
"""


def get_app() -> FastAPI:
    return create_app()


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:get_app",
        factory=True,
        host=s.admin_host,
        port=s.admin_port,
        workers=1,
        log_level=s.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
