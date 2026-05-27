"""Client REST OpenHAB minimal: legge i valori degli items Deye in batch."""
from __future__ import annotations

import logging
import math
from typing import Any

import httpx

log = logging.getLogger(__name__)


NULL_STATES = {"NULL", "UNDEF", None, ""}

# Tutti gli items Deye che ci servono dal binding modbus su OpenHAB.
DEYE_ITEMS = [
    # Inverter (AC output) — sui registri 627-636 lato Deye
    "DeyeModbusInverterAVoltage",
    "DeyeModbusInverterBVoltage",
    "DeyeModbusInverterCVoltage",
    "DeyeModbusInverterACurrent",
    "DeyeModbusInverterBCurrent",
    "DeyeModbusInverterCCurrent",
    "DeyeModbusInverterAPower",
    "DeyeModbusInverterBPower",
    "DeyeModbusInverterCPower",
    "DeyeModbusInverterTotal",
    # PV (input DC)
    "DeyeModbusPv1Power",
    "DeyeModbusPv2Power",
    "DeyeModbusPvPower",
    # Energia
    "DeyeModbusProdDaily",
    "DeyeModbusProdTotal",
    "DeyeModbusProdTotalHi",
    "DeyeModbusProdTotalLo",
    # Temperature
    "DeyeModbusAcTemp",
    "DeyeModbusDcTemp",
    "DeyeModbusBatteryTemp",
    # Battery
    "DeyeModbusBatterySoc",
    # Grid (per meter virtuale 37100+)
    "DeyeModbusGridAPower",
    "DeyeModbusGridBPower",
    "DeyeModbusGridCPower",
    "DeyeModbusGridACurrent",
    "DeyeModbusGridBCurrent",
    "DeyeModbusGridCCurrent",
    "DeyeModbusGridTotal",
    # Consumo casa
    "DeyeModbusLoadTotal",
]


# MOCK GIORNO SURPLUS ALTO: PV >> AC_real (batteria carica con surplus).
# Test diagnostico per scoprire formula Viaris quando display Home=0.
#
# Scenario REALE:
#   PV_real          = 5000 W (sole alto)
#   AC_inverter_real = 500 W (inverter eroga poco a casa)
#   Battery_real     = +4500 W (= PV-AC, surplus va in batteria)
#   Grid_real        = 0 W (autoconsumo perfetto)
#   Load_real        = 500 W (consumo casa basso)
#
# Mock SCRITTO con clamp:
#   AC_mock (32080)  = (5000+500)/2 = 2750 W
#   37001 clamped    = min(4500, 2750) = +2750
#   37743 clamped    = +2750
#   I_phase inverter = 2750/3/230 ~= 4 A
#
# PREDIZIONI Viaris (se formula day come Round 1-5):
#   Solar   = 5.0 kW
#   Battery = 2*(5000-2750) = 4500 -> 4.5 kW CHARGING
#   Home    = 2*2750 - 5000 - 0 = 500 -> 0.5 kW (= Load reale)
#   Rete    = 0
#   SoC     = 75%
# Se Home != 0.5 -> formula day diversa, manda screenshot per scoprirla.
MOCK_ITEMS: dict[str, float] = {
    "DeyeModbusPv1Power": 2500.0,
    "DeyeModbusPv2Power": 2500.0,
    "DeyeModbusPvPower": 5000.0,
    # Inverter AC eroga solo 500 W (surplus carica batteria)
    "DeyeModbusInverterAPower": 170.0,
    "DeyeModbusInverterBPower": 170.0,
    "DeyeModbusInverterCPower": 160.0,
    "DeyeModbusInverterTotal": 500.0,
    "DeyeModbusInverterACurrent": 0.7,
    "DeyeModbusInverterBCurrent": 0.7,
    "DeyeModbusInverterCCurrent": 0.7,
    "DeyeModbusInverterAVoltage": 230.0,
    "DeyeModbusInverterBVoltage": 230.0,
    "DeyeModbusInverterCVoltage": 230.0,
    # Grid meter: nessuno scambio (autoconsumo perfetto)
    "DeyeModbusGridTotal": 0.0,
    "DeyeModbusGridAPower": 0.0,
    "DeyeModbusGridBPower": 0.0,
    "DeyeModbusGridCPower": 0.0,
    "DeyeModbusGridACurrent": 0.0,
    "DeyeModbusGridBCurrent": 0.0,
    "DeyeModbusGridCCurrent": 0.0,
    # Load = 500 W (consumo casa basso, surplus va in batt)
    "DeyeModbusLoadTotal": 500.0,
    # SoC: 75%
    "DeyeModbusBatterySoc": 75.0,
    "DeyeModbusBatteryTemp": 28.0,
    "DeyeModbusAcTemp": 35.0,
    "DeyeModbusDcTemp": 45.0,
    "DeyeModbusProdDaily": 12.34,
    "DeyeModbusProdTotal": 1.234,
}


def parse_number(raw: Any) -> float | None:
    """Best-effort: ritorna None se NULL/UNDEF/empty/non-numeric."""
    if raw in NULL_STATES:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if math.isfinite(v) else None
    try:
        s = str(raw).strip()
        if s in NULL_STATES:
            return None
        first = s.split()[0]
        v = float(first)
        return v if math.isfinite(v) else None
    except (ValueError, IndexError):
        return None


class OpenHabClient:
    def __init__(self, base_url: str, timeout_s: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._wanted = set(DEYE_ITEMS)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_all(self) -> dict[str, float | None]:
        """Una sola chiamata batch a /rest/items, filtra i wanted, parsifica.

        Ritorna dict {item_name: float_or_None}. Items missing dall'OH non
        appaiono nel dict.
        """
        url = f"{self._base}/rest/items"
        resp = await self._client.get(url, params={"fields": "name,state"})
        resp.raise_for_status()
        payload = resp.json()
        out: dict[str, float | None] = {}
        for it in payload:
            name = it.get("name")
            if name in self._wanted:
                out[name] = parse_number(it.get("state"))
        return out
