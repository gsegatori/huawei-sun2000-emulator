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


# Valori MOCK riconoscibili a colpo d'occhio: ogni grandezza ha cifre
# univoche e crescenti, cosi' guardando l'app/UI client si capisce dove
# l'integrazione mappa ciascun registro.
MOCK_ITEMS: dict[str, float] = {
    # PV input DC (32064) -> 1234 W (1.234 kW) = "Solar" attesa
    "DeyeModbusPv1Power": 600.0,
    "DeyeModbusPv2Power": 634.0,
    "DeyeModbusPvPower": 1234.0,
    # Inverter AC output (32080) -> 2345 W (2.345 kW) = "Inst. power"
    "DeyeModbusInverterAPower": 800.0,
    "DeyeModbusInverterBPower": 750.0,
    "DeyeModbusInverterCPower": 795.0,
    "DeyeModbusInverterTotal": 2345.0,
    "DeyeModbusInverterACurrent": 1.0,
    "DeyeModbusInverterBCurrent": 2.0,
    "DeyeModbusInverterCCurrent": 3.0,
    "DeyeModbusInverterAVoltage": 230.0,
    "DeyeModbusInverterBVoltage": 231.0,
    "DeyeModbusInverterCVoltage": 232.0,
    # Grid meter (37113) -> 3456 W (3.456 kW import) = "Rete"
    "DeyeModbusGridTotal": 3456.0,
    "DeyeModbusGridAPower": 1000.0,
    "DeyeModbusGridBPower": 1200.0,
    "DeyeModbusGridCPower": 1256.0,
    "DeyeModbusGridACurrent": 5.1,
    "DeyeModbusGridBCurrent": 5.2,
    "DeyeModbusGridCCurrent": 5.3,
    # Load house -> 4567 W = "Casa"
    "DeyeModbusLoadTotal": 4567.0,
    # Battery SoC 50%, derivato charge_p = pv - inverter = 1234 - 2345 = -1111 W (scarica)
    "DeyeModbusBatterySoc": 50.0,
    "DeyeModbusBatteryTemp": 25.0,
    "DeyeModbusAcTemp": 33.0,
    "DeyeModbusDcTemp": 44.0,
    # Energy: 12.34 kWh daily, 1.234 MWh total
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
