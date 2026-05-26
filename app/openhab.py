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


# Valori MOCK fisicamente COERENTI (bilancio energia rispettato) e con
# numeri DISTINTI per ogni grandezza, cosi' guardando l'app/UI client si
# capisce a colpo d'occhio dove ciascun registro finisce.
#
# Scenario simulato:
#   PV produce 9000 W -> 6000 W vanno all'inverter (verso casa+rete),
#                        3000 W caricano la batteria
#   Inverter eroga 6000 W AC -> 4000 W consumati da casa, 2000 W esportati
#   Grid Total = -2000 W (negativo = export verso rete)
#
# Bilancio: PV 9000 = AC_out 6000 + Battery_charge 3000  ✓
#           AC_out 6000 = Load 4000 + Grid_export 2000  ✓
#
# Numeri attesi nella Viaris (kW): Solar=9, Inst.power=6, Battery=3 charging,
# Rete=-2 export, Casa=4. Tutti distinti = mapping inequivocabile.
MOCK_ITEMS: dict[str, float] = {
    # PV input DC (32064) -> 9000 W = "Solar" atteso 9.0 kW
    "DeyeModbusPv1Power": 4500.0,
    "DeyeModbusPv2Power": 4500.0,
    "DeyeModbusPvPower": 9000.0,
    # Inverter AC output (32080) -> 6000 W = "Inst. power" atteso 6.0 kW
    "DeyeModbusInverterAPower": 2000.0,
    "DeyeModbusInverterBPower": 2000.0,
    "DeyeModbusInverterCPower": 2000.0,
    "DeyeModbusInverterTotal": 6000.0,
    # Correnti inverter univocamente identificabili per fase
    "DeyeModbusInverterACurrent": 9.0,
    "DeyeModbusInverterBCurrent": 8.7,
    "DeyeModbusInverterCCurrent": 8.3,
    # Voltages distinti per fase
    "DeyeModbusInverterAVoltage": 220.0,
    "DeyeModbusInverterBVoltage": 230.0,
    "DeyeModbusInverterCVoltage": 240.0,
    # Grid meter (37113) -> -2000 W = "Rete" atteso -2.0 kW (export)
    "DeyeModbusGridTotal": -2000.0,
    "DeyeModbusGridAPower": -667.0,
    "DeyeModbusGridBPower": -667.0,
    "DeyeModbusGridCPower": -666.0,
    "DeyeModbusGridACurrent": 3.0,
    "DeyeModbusGridBCurrent": 2.9,
    "DeyeModbusGridCCurrent": 2.8,
    # Load house -> 4000 W = "Casa" atteso 4.0 kW
    "DeyeModbusLoadTotal": 4000.0,
    # Battery: SoC 75% (univoco) e charge_p derivato = PV - Inverter = 3000 W (carica)
    "DeyeModbusBatterySoc": 75.0,
    "DeyeModbusBatteryTemp": 28.0,
    "DeyeModbusAcTemp": 35.0,
    "DeyeModbusDcTemp": 45.0,
    # Energy distintivi: 56.78 kWh daily, 4.321 MWh total
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
