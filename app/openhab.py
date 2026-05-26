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


# ROUND 2 — test diagnostico: cambio SOLO il PV (32064) per confermare Solar
# e capire se Home/Rete dipendono da PV o da altro.
#
# Conclusione Round 1: "Battery" della Viaris e' CALCOLATO dal bilancio
# Solar - Home + Rete_export. NON e' letto dai registri 32080/37001/37750.
#
# Cambia SOLO PV rispetto a Round 1:
#   PV: 9000 -> 7777 W  <-- valore-spia univoco
#   Inverter AC out = 4321 W (INVARIATO)
#   Load = 2321 W (INVARIATO)
#   Grid = -2000 W (INVARIATO)
#   Battery_charge derivato = 7777 - 4321 = 3456 W (cambia per coerenza)
#
# PREDIZIONI Viaris display dopo questo Round:
#   Solar       = 7.78 kW (se segue 32064 come ipotizzato)
#   Battery     = Solar - Home + Rete_export = 7.78 - 0.49 + 0.66 = 7.95 kW
#                (se la Viaris fa lo stesso bilancio del Round 1)
#   Home        = 0.49 kW invariato (se non dipende da PV)
#   Rete        = 0.66 kW invariato (se non dipende da PV)
#   SoC         = 75% invariato
MOCK_ITEMS: dict[str, float] = {
    # PV input DC - VALORE SPIA Round 2: 7777 W
    "DeyeModbusPv1Power": 3888.0,
    "DeyeModbusPv2Power": 3889.0,
    "DeyeModbusPvPower": 7777.0,
    # Inverter AC output INVARIATO (Round 1)
    "DeyeModbusInverterAPower": 1421.0,
    "DeyeModbusInverterBPower": 1450.0,
    "DeyeModbusInverterCPower": 1450.0,
    "DeyeModbusInverterTotal": 4321.0,
    "DeyeModbusInverterACurrent": 6.5,
    "DeyeModbusInverterBCurrent": 6.3,
    "DeyeModbusInverterCCurrent": 6.0,
    "DeyeModbusInverterAVoltage": 220.0,
    "DeyeModbusInverterBVoltage": 230.0,
    "DeyeModbusInverterCVoltage": 240.0,
    # Grid meter INVARIATO (-2000 export)
    "DeyeModbusGridTotal": -2000.0,
    "DeyeModbusGridAPower": -700.0,
    "DeyeModbusGridBPower": -600.0,
    "DeyeModbusGridCPower": -700.0,
    "DeyeModbusGridACurrent": 3.0,
    "DeyeModbusGridBCurrent": 2.9,
    "DeyeModbusGridCCurrent": 2.8,
    # Load INVARIATO
    "DeyeModbusLoadTotal": 2321.0,
    # Battery: SoC 75% invariato; battery_charge derivato = 7777-4321 = 3456 W
    "DeyeModbusBatterySoc": 75.0,
    "DeyeModbusBatteryTemp": 28.0,
    "DeyeModbusAcTemp": 35.0,
    "DeyeModbusDcTemp": 45.0,
    "DeyeModbusProdDaily": 56.78,
    "DeyeModbusProdTotal": 4.321,
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
