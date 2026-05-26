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


# ROUND 4 — TEST IMBROGLIO: target battery scarica 0.9 kW.
#
# FORMULE VIARIS confermate nei round 1-3:
#   Solar_display = PV_mock (32064)
#   Battery_display = 2 × (PV_mock - AC_mock)
#   Home_display = 2×AC_mock - PV_mock + |Grid_A_phase_mock|
#   Rete_display = |Grid_A_phase_mock|
#   SoC_display = 37738
#
# Per far apparire BATTERY CORRETTA, "imbroghiamo" AC_mock:
#   AC_mock = (PV_real + AC_inverter_real) / 2
# Cosi' la formula 2×(PV-AC) restituisce battery_real.
#
# Scenario simulato (REALI):
#   PV_real            = 1200 W (Solar atteso 1.2 kW)
#   AC_inverter_real   = 2100 W
#   Battery_real       = PV-AC = -900 W (scarica)
#   Grid_real          = +600 W (import)
#   Load_real          = 2700 W (casa consuma)
#
# Mock IMBROGLIATI:
#   PV_mock     = 1200 (= PV_real)
#   AC_mock     = (1200+2100)/2 = 1650
#   Grid_A_mock = 600 (= |Grid_real|, solo fase A)
#
# PREDIZIONI Viaris display:
#   Solar      = 1.2 kW
#   Battery    = 2×(1200-1650) = -900 -> 0.9 kW DISCHARGING
#   Home       = 2×1650 - 1200 + 600 = 2700 -> 2.7 kW
#   Rete       = 0.6 kW
#   SoC        = 75%
MOCK_ITEMS: dict[str, float] = {
    # PV input DC: PV_real = 1200 W
    "DeyeModbusPv1Power": 600.0,
    "DeyeModbusPv2Power": 600.0,
    "DeyeModbusPvPower": 1200.0,
    # Inverter AC output IMBROGLIATO a 1650 W (= (PV_real+AC_real)/2)
    "DeyeModbusInverterAPower": 550.0,
    "DeyeModbusInverterBPower": 550.0,
    "DeyeModbusInverterCPower": 550.0,
    "DeyeModbusInverterTotal": 1650.0,
    "DeyeModbusInverterACurrent": 2.5,
    "DeyeModbusInverterBCurrent": 2.4,
    "DeyeModbusInverterCCurrent": 2.3,
    "DeyeModbusInverterAVoltage": 220.0,
    "DeyeModbusInverterBVoltage": 230.0,
    "DeyeModbusInverterCVoltage": 240.0,
    # Grid meter: import +600 W solo fase A
    "DeyeModbusGridTotal": 600.0,
    "DeyeModbusGridAPower": 600.0,
    "DeyeModbusGridBPower": 0.0,
    "DeyeModbusGridCPower": 0.0,
    "DeyeModbusGridACurrent": 2.7,
    "DeyeModbusGridBCurrent": 0.0,
    "DeyeModbusGridCCurrent": 0.0,
    # Load 2700 W (= casa)
    "DeyeModbusLoadTotal": 2700.0,
    # SoC: 75% (invariato), Battery temp/Inverter temp invariati
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
