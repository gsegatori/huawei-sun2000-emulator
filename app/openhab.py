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


# ROUND 1 — test diagnostico: isolare la fonte del campo "Battery" Viaris.
#
# Hp da verificare: "Battery" della Viaris segue 32080 (Active Power AC)?
#
# Scenario simulato (cambia SOLO active_power rispetto al round precedente,
# tutto il resto resta uguale per non confondere):
#   PV produce 9000 W
#   Inverter AC out = 4321 W (NUOVO, era 6000)  <-- valore-spia univoco
#   Battery_charge = PV - AC = 9000 - 4321 = 4679 W (verra' scritto a 37001)
#   Load = 2321 W  (= AC - Grid_export = 4321 - 2000)
#   Grid_total = -2000 W (export, invariato)
#
# Bilancio: 9000 = 4321 + 4679 ✓ ; 4321 = 2321 + 2000 ✓
#
# PREDIZIONI Viaris display:
#   Solar          = 9.0 kW (legge 32064 -> invariato)
#   Battery        =
#       SE 4.32 kW -> conferma 32080 (Active Power) come fonte (bug Viaris)
#       SE 4.68 kW -> conferma 37001 (battery_charge_discharge_power)
#       SE 6.0 kW  -> qualche cache, niente cambio
#       SE altro   -> da indagare
#   Casa           = 2.32 kW (se derivato bilancio), ~ Load 2.32
#   Rete           = -2.0 kW (Grid invariato)
MOCK_ITEMS: dict[str, float] = {
    # PV input DC invariato
    "DeyeModbusPv1Power": 4500.0,
    "DeyeModbusPv2Power": 4500.0,
    "DeyeModbusPvPower": 9000.0,
    # Inverter AC output - VALORE SPIA Round 1: 4321 W
    "DeyeModbusInverterAPower": 1421.0,  # somma totale = 4321
    "DeyeModbusInverterBPower": 1450.0,
    "DeyeModbusInverterCPower": 1450.0,
    "DeyeModbusInverterTotal": 4321.0,   # <-- valore-spia
    "DeyeModbusInverterACurrent": 6.5,
    "DeyeModbusInverterBCurrent": 6.3,
    "DeyeModbusInverterCCurrent": 6.0,
    "DeyeModbusInverterAVoltage": 220.0,
    "DeyeModbusInverterBVoltage": 230.0,
    "DeyeModbusInverterCVoltage": 240.0,
    # Grid meter invariato a -2000 W (export)
    "DeyeModbusGridTotal": -2000.0,
    "DeyeModbusGridAPower": -700.0,
    "DeyeModbusGridBPower": -600.0,
    "DeyeModbusGridCPower": -700.0,
    "DeyeModbusGridACurrent": 3.0,
    "DeyeModbusGridBCurrent": 2.9,
    "DeyeModbusGridCCurrent": 2.8,
    # Load house: 2321 W (= 4321 - 2000)
    "DeyeModbusLoadTotal": 2321.0,
    # Battery: SoC 75% invariato, charge_p derivato in apply_values = 9000 - 4321 = 4679
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
