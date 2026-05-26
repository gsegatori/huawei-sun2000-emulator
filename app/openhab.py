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
