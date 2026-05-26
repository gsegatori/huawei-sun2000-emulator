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


# MOCK ITEMS: valori "fisici" (PRE-imbroglio). L'imbroglio AC=(PV+AC)/2
# viene applicato dal poller in apply_values(), quindi qui mettiamo i
# valori REALI come se fossero letti dal Deye.
#
# Scenario REALE (autoconsumo + surplus carica batteria):
#   PV_real          = 4000 W (Solar atteso 4.0 kW)
#   AC_inverter_real = 3500 W
#   Battery_real     = PV - AC = +500 W (carica)
#   Grid_real        = 0 W (no scambio rete)
#   Load_real        = 3500 W
#
# Il poller scrivera' a 32080 il valore imbrogliato = (4000+3500)/2 = 3750.
# La Viaris calcolera':
#   Solar   = 4.0 kW (legge 32064 = PV reale)
#   Battery = 2×(4000-3750) = 500 -> 0.5 kW CHARGING
#   Home    = 2×3750 - 4000 - 0 = 3500 -> 3.5 kW  (= Load_real!)
#   Rete    = 0 kW
#   SoC     = 50%
MOCK_ITEMS: dict[str, float] = {
    # PV input DC: PV_real = 4000 W
    "DeyeModbusPv1Power": 2000.0,
    "DeyeModbusPv2Power": 2000.0,
    "DeyeModbusPvPower": 4000.0,
    # Inverter AC output FISICO = 3500 W (il poller imbroglia automaticamente)
    "DeyeModbusInverterAPower": 1170.0,
    "DeyeModbusInverterBPower": 1170.0,
    "DeyeModbusInverterCPower": 1160.0,
    "DeyeModbusInverterTotal": 3500.0,
    "DeyeModbusInverterACurrent": 5.4,
    "DeyeModbusInverterBCurrent": 5.4,
    "DeyeModbusInverterCCurrent": 5.4,
    "DeyeModbusInverterAVoltage": 230.0,
    "DeyeModbusInverterBVoltage": 230.0,
    "DeyeModbusInverterCVoltage": 230.0,
    # Grid meter: nessuno scambio
    "DeyeModbusGridTotal": 0.0,
    "DeyeModbusGridAPower": 0.0,
    "DeyeModbusGridBPower": 0.0,
    "DeyeModbusGridCPower": 0.0,
    "DeyeModbusGridACurrent": 0.0,
    "DeyeModbusGridBCurrent": 0.0,
    "DeyeModbusGridCCurrent": 0.0,
    # Load = 3500 W (= output inverter reale, no scambio rete)
    "DeyeModbusLoadTotal": 3500.0,
    # SoC: 50% (distinto dai round precedenti)
    "DeyeModbusBatterySoc": 50.0,
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
