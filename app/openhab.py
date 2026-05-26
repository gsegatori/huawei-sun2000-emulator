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


# ROUND 3 — test diagnostico: cambio SOLO Grid (37113) per isolare 'Rete'.
#
# Conclusioni Round 1+2:
#  - Solar = 32064 (Input Power DC) — conferma
#  - SoC = 37738 — conferma
#  - Battery = formula calcolata = Solar - Home + Rete_export
#  - Home dipende anche dal PV (composita)
#
# Cambia SOLO Grid_total rispetto a Round 2:
#   PV = 7777 W (INVARIATO)
#   Inverter AC = 4321 W (INVARIATO)
#   Grid_total = -2345 W (NUOVO, era -2000)  <-- valore-spia
#   Grid per fase: A=-1000, B=-700, C=-645 (sum -2345, DISTINTI per indagare phase)
#   Load = 4321 - 2345 = 1976 W (per mantenere bilancio AC=Load+GridExport)
#   Battery_charge = 7777 - 4321 = 3456 W (invariato)
#
# PREDIZIONI Viaris display:
#   Solar      = 7.82 kW (PV invariato)
#   Rete (Inst.power) =
#       SE 0.78 kW -> Rete = |Grid_total|/3 = 2345/3 = 781 (per-fase media)
#       SE 1.00 kW -> Rete = |Grid_A_phase|/1 = 1000 (fase A specifica)
#       SE 2.35 kW -> Rete = |Grid_total|/1 = 2345 (totale)
#       SE altro   -> da indagare
#   Battery    = Solar - Home + Rete (formula confermata)
#   Home       = derivato (cambia con Grid?)
MOCK_ITEMS: dict[str, float] = {
    # PV input DC INVARIATO (Round 2)
    "DeyeModbusPv1Power": 3888.0,
    "DeyeModbusPv2Power": 3889.0,
    "DeyeModbusPvPower": 7777.0,
    # Inverter AC output INVARIATO
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
    # Grid meter - VALORE SPIA Round 3: -2345 W, fasi DISTINTE
    "DeyeModbusGridTotal": -2345.0,
    "DeyeModbusGridAPower": -1000.0,  # fase A grossa
    "DeyeModbusGridBPower": -700.0,   # fase B media
    "DeyeModbusGridCPower": -645.0,   # fase C piccola (sum = -2345)
    "DeyeModbusGridACurrent": 4.5,
    "DeyeModbusGridBCurrent": 3.0,
    "DeyeModbusGridCCurrent": 2.8,
    # Load: 1976 W (per mantenere bilancio AC = Load + Grid_export)
    "DeyeModbusLoadTotal": 1976.0,
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
