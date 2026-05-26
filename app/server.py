"""Server Modbus TCP che impersona un Huawei SUN2000.

Architettura:
- ModbusSparseDataBlock con i registri pre-allocati a 0
- ModbusSlaveContext che espone i registri come Holding Registers (FC=3)
- Update background task che ogni POLL_INTERVAL_S legge OH e fa setValues()
- Server gestito in lifecycle async, condivide il loop con FastAPI admin UI
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSparseDataBlock
from pymodbus.server import StartAsyncTcpServer

from app import registers as R
from app.config import Settings
from app.openhab import OpenHabClient

log = logging.getLogger(__name__)


class HuaweiModbusEmulator:
    """Tiene il datastore Modbus, popola i registri identity al boot, espone
    update_from_openhab() per il poller.

    Cosi' separato dal lifecycle del server TCP -> facile da testare in unit
    test senza dover bindare la porta 502.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Allochiamo tutti i registri che useremo a 0. ModbusSparseDataBlock
        # accetta dict {addr: value} oppure {addr: [values]}. Per semplicita'
        # usiamo dict di singoli zero, poi setValues() popola i runtime.
        all_addrs = self._all_used_addresses()
        initial = {addr: 0 for addr in all_addrs}
        self._block = ModbusSparseDataBlock(initial)
        self._slave = ModbusSlaveContext(hr=self._block, zero_mode=True)
        self.context = ModbusServerContext(slaves={settings.modbus_unit_id: self._slave}, single=False)
        self._populate_identity()
        self._last_update_ts: float = 0.0
        self._last_update_ok: bool = False
        self._last_error: str | None = None

    # ───── debug / introspection ─────

    @property
    def last_update_ts(self) -> float:
        return self._last_update_ts

    @property
    def last_update_ok(self) -> bool:
        return self._last_update_ok

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def read_register(self, address: int, count: int = 1) -> list[int]:
        return list(self._block.getValues(address, count))

    # ───── implementation ─────

    def _all_used_addresses(self) -> set[int]:
        """Tutte le address con dimensioni dovute (U32/I32 occupa 2)."""
        s: set[int] = set()
        # device info
        for addr, span in [
            (R.ADDR_MODEL_NAME, 15),
            (R.ADDR_SN, 10),
            (R.ADDR_PN, 10),
            (R.ADDR_FIRMWARE_VERSION, 15),
            (R.ADDR_NUMBER_OF_PV_STRINGS, 1),
            (R.ADDR_RATED_POWER, 2),
            (R.ADDR_MAX_ACTIVE_POWER, 2),
            (R.ADDR_MAX_APPARENT_POWER, 2),
            (R.ADDR_MAX_REACTIVE_POWER_PER_QUADRANT, 2),
        ]:
            for i in range(span):
                s.add(addr + i)
        # realtime
        for addr, span in [
            (R.ADDR_STATE_1, 1),
            (R.ADDR_STATE_2, 1),
            (R.ADDR_STATE_3, 2),
            (R.ADDR_ALARM_1, 1),
            (R.ADDR_ALARM_2, 1),
            (R.ADDR_ALARM_3, 1),
            (R.ADDR_PV1_VOLTAGE, 1),
            (R.ADDR_PV1_CURRENT, 1),
            (R.ADDR_PV2_VOLTAGE, 1),
            (R.ADDR_PV2_CURRENT, 1),
            (R.ADDR_INPUT_POWER, 2),
            (R.ADDR_GRID_VOLTAGE_AB, 1),
            (R.ADDR_GRID_VOLTAGE_BC, 1),
            (R.ADDR_GRID_VOLTAGE_CA, 1),
            (R.ADDR_GRID_VOLTAGE_A, 1),
            (R.ADDR_GRID_VOLTAGE_B, 1),
            (R.ADDR_GRID_VOLTAGE_C, 1),
            (R.ADDR_GRID_CURRENT_A, 2),
            (R.ADDR_GRID_CURRENT_B, 2),
            (R.ADDR_GRID_CURRENT_C, 2),
            (R.ADDR_PEAK_ACTIVE_POWER_DAY, 2),
            (R.ADDR_ACTIVE_POWER, 2),
            (R.ADDR_REACTIVE_POWER, 2),
            (R.ADDR_POWER_FACTOR, 1),
            (R.ADDR_GRID_FREQUENCY, 1),
            (R.ADDR_EFFICIENCY, 1),
            (R.ADDR_INTERNAL_TEMPERATURE, 1),
            (R.ADDR_INSULATION_RESISTANCE, 1),
            (R.ADDR_DEVICE_STATUS, 1),
            (R.ADDR_FAULT_CODE, 1),
            (R.ADDR_ACCUMULATED_YIELD, 2),
            (R.ADDR_DAILY_YIELD, 2),
        ]:
            for i in range(span):
                s.add(addr + i)
        # battery
        for addr, span in [
            (R.ADDR_BATTERY_RUNNING_STATUS, 1),
            (R.ADDR_BATTERY_CHARGE_DISCHARGE_POWER, 2),
            (R.ADDR_BATTERY_SOC, 1),
            (R.ADDR_BATTERY_TEMPERATURE, 1),
            (R.ADDR_BATTERY_TOTAL_CHARGE, 2),
            (R.ADDR_BATTERY_TOTAL_DISCHARGE, 2),
        ]:
            for i in range(span):
                s.add(addr + i)
        # meter
        for addr, span in [
            (R.ADDR_METER_STATUS, 1),
            (R.ADDR_METER_GRID_VOLTAGE_A, 2),
            (R.ADDR_METER_GRID_VOLTAGE_B, 2),
            (R.ADDR_METER_GRID_VOLTAGE_C, 2),
            (R.ADDR_METER_GRID_CURRENT_A, 2),
            (R.ADDR_METER_GRID_CURRENT_B, 2),
            (R.ADDR_METER_GRID_CURRENT_C, 2),
            (R.ADDR_METER_ACTIVE_POWER, 2),
            (R.ADDR_METER_REACTIVE_POWER, 2),
            (R.ADDR_METER_POWER_FACTOR, 1),
            (R.ADDR_METER_FREQUENCY, 1),
            (R.ADDR_METER_POSITIVE_ACTIVE_ENERGY, 2),
            (R.ADDR_METER_REVERSE_ACTIVE_ENERGY, 2),
        ]:
            for i in range(span):
                s.add(addr + i)
        return s

    def _populate_identity(self) -> None:
        s = self._settings
        self._block.setValues(R.ADDR_MODEL_NAME, R.string_regs(s.huawei_model, 15))
        self._block.setValues(R.ADDR_SN, R.string_regs(s.huawei_sn, 10))
        self._block.setValues(R.ADDR_PN, R.string_regs(s.huawei_pn, 10))
        self._block.setValues(R.ADDR_FIRMWARE_VERSION, R.string_regs(s.huawei_fw, 15))
        self._block.setValues(R.ADDR_NUMBER_OF_PV_STRINGS, R.u16(s.huawei_pv_strings))
        self._block.setValues(R.ADDR_RATED_POWER, R.u32(s.huawei_rated_power_w))
        self._block.setValues(R.ADDR_MAX_ACTIVE_POWER, R.u32(s.huawei_max_active_power_w))
        self._block.setValues(R.ADDR_MAX_APPARENT_POWER, R.u32(s.huawei_max_apparent_power_va))
        self._block.setValues(R.ADDR_MAX_REACTIVE_POWER_PER_QUADRANT, R.i32(0))
        # default constants
        self._block.setValues(R.ADDR_GRID_FREQUENCY, R.u16(5000))   # 50.00 Hz
        self._block.setValues(R.ADDR_EFFICIENCY, R.u16(9800))        # 98.00 %
        self._block.setValues(R.ADDR_INSULATION_RESISTANCE, R.u16(30000))  # 30 MΩ
        self._block.setValues(R.ADDR_POWER_FACTOR, R.i16(1000))     # 1.000
        self._block.setValues(R.ADDR_METER_STATUS, R.u16(1))         # normal
        self._block.setValues(R.ADDR_METER_FREQUENCY, R.i16(5000))
        self._block.setValues(R.ADDR_METER_POWER_FACTOR, R.i16(1000))
        if s.huawei_has_battery:
            self._block.setValues(R.ADDR_BATTERY_RUNNING_STATUS, R.u16(2))  # running
        else:
            self._block.setValues(R.ADDR_BATTERY_RUNNING_STATUS, R.u16(0))  # offline

    def apply_values(self, items: dict[str, float | None]) -> None:
        """Aggiorna i registri runtime dai valori OH (gia' parsed)."""
        g = items.get  # shortcut
        b = self._block

        # PV input
        pv1 = g("DeyeModbusPv1Power") or 0
        pv2 = g("DeyeModbusPv2Power") or 0
        pv_tot = g("DeyeModbusPvPower")
        if pv_tot is None:
            pv_tot = pv1 + pv2

        # Assumption: tensione PV ~400V, corrente = P/V (approx). Senza dato preciso.
        # Se vuoi dati piu' fedeli, leggere i registri Deye specifici PV V/I.
        for addr_v, addr_i, p in (
            (R.ADDR_PV1_VOLTAGE, R.ADDR_PV1_CURRENT, pv1),
            (R.ADDR_PV2_VOLTAGE, R.ADDR_PV2_CURRENT, pv2),
        ):
            v_dv = 4000  # 400.0 V * 10
            i_ca = int((p / 400.0) * 100) if p > 0 else 0
            b.setValues(addr_v, R.i16(v_dv))
            b.setValues(addr_i, R.i16(i_ca))

        b.setValues(R.ADDR_INPUT_POWER, R.i32(int(pv_tot)))

        # Grid voltage (V * 10) -- usiamo le tensioni d'inverter (sulla stessa linea)
        va = g("DeyeModbusInverterAVoltage") or 230
        vb = g("DeyeModbusInverterBVoltage") or 230
        vc = g("DeyeModbusInverterCVoltage") or 230
        b.setValues(R.ADDR_GRID_VOLTAGE_A, R.u16(int(va * 10)))
        b.setValues(R.ADDR_GRID_VOLTAGE_B, R.u16(int(vb * 10)))
        b.setValues(R.ADDR_GRID_VOLTAGE_C, R.u16(int(vc * 10)))
        # Vab/Vbc/Vca: linea-linea = sqrt(3) * fase, approssimato
        b.setValues(R.ADDR_GRID_VOLTAGE_AB, R.u16(int(((va + vb) / 2) * 10 * 1.732)))
        b.setValues(R.ADDR_GRID_VOLTAGE_BC, R.u16(int(((vb + vc) / 2) * 10 * 1.732)))
        b.setValues(R.ADDR_GRID_VOLTAGE_CA, R.u16(int(((vc + va) / 2) * 10 * 1.732)))

        # Grid current (A * 1000)
        ia = g("DeyeModbusInverterACurrent") or 0
        ib = g("DeyeModbusInverterBCurrent") or 0
        ic = g("DeyeModbusInverterCCurrent") or 0
        b.setValues(R.ADDR_GRID_CURRENT_A, R.i32(int(ia * 1000)))
        b.setValues(R.ADDR_GRID_CURRENT_B, R.i32(int(ib * 1000)))
        b.setValues(R.ADDR_GRID_CURRENT_C, R.i32(int(ic * 1000)))

        # Active power AC (output inverter, W)
        active_total = g("DeyeModbusInverterTotal")
        if active_total is None:
            # fallback: somma per fase
            pa = g("DeyeModbusInverterAPower") or 0
            pb = g("DeyeModbusInverterBPower") or 0
            pc = g("DeyeModbusInverterCPower") or 0
            active_total = pa + pb + pc
        b.setValues(R.ADDR_ACTIVE_POWER, R.i32(int(active_total)))

        # Reactive power: nessun dato in OH -> 0
        b.setValues(R.ADDR_REACTIVE_POWER, R.i32(0))

        # Internal temperature (°C * 10)
        ac_temp = g("DeyeModbusAcTemp") or g("DeyeModbusDcTemp") or 25
        b.setValues(R.ADDR_INTERNAL_TEMPERATURE, R.i16(int(ac_temp * 10)))

        # Device status: running se PV>50W, altrimenti standby_no_irradiation
        status = R.STATUS_ON_GRID_RUNNING if pv_tot > 50 else R.STATUS_STANDBY_NO_IRRADIATION
        b.setValues(R.ADDR_DEVICE_STATUS, R.u16(status))
        b.setValues(R.ADDR_STATE_1, R.u16(0x0001 if status == R.STATUS_ON_GRID_RUNNING else 0))

        # Energy: daily (kWh * 100) e accumulated (kWh * 100)
        daily_kwh = g("DeyeModbusProdDaily") or 0
        b.setValues(R.ADDR_DAILY_YIELD, R.u32(int(daily_kwh * 100)))
        # ProdTotal e' in MWh -> convertiamo a kWh*100
        prod_total_mwh = g("DeyeModbusProdTotal")
        if prod_total_mwh is None:
            # fallback dai due 16-bit raw: (Hi*65536+Lo)/10 = kWh
            hi = g("DeyeModbusProdTotalHi") or 0
            lo = g("DeyeModbusProdTotalLo") or 0
            kwh = (int(hi) * 65536 + int(lo)) / 10
        else:
            kwh = prod_total_mwh * 1000
        b.setValues(R.ADDR_ACCUMULATED_YIELD, R.u32(int(kwh * 100)))

        # Battery
        if self._settings.huawei_has_battery:
            soc = g("DeyeModbusBatterySoc") or 0
            btemp = g("DeyeModbusBatteryTemp") or 25
            b.setValues(R.ADDR_BATTERY_SOC, R.u16(int(soc * 10)))
            b.setValues(R.ADDR_BATTERY_TEMPERATURE, R.i16(int(btemp * 10)))
            # Battery power (convenzione Huawei: + = carica, - = scarica).
            # Bilancio AC inverter ibrido: P_pv = P_inverter_out + P_batt_charge
            # quindi P_batt = P_pv - P_inverter_out (se PV avanza si carica,
            # se l'inverter eroga piu' del PV la batteria si scarica).
            charge_p = int((pv_tot or 0) - (active_total or 0))
            b.setValues(R.ADDR_BATTERY_CHARGE_DISCHARGE_POWER, R.i32(charge_p))

        # Smart meter (37100+) — sostituisce il meter Huawei DDSU666 collegato all'inverter
        gva = int(va * 10)
        gvb = int(vb * 10)
        gvc = int(vc * 10)
        b.setValues(R.ADDR_METER_GRID_VOLTAGE_A, R.i32(gva))
        b.setValues(R.ADDR_METER_GRID_VOLTAGE_B, R.i32(gvb))
        b.setValues(R.ADDR_METER_GRID_VOLTAGE_C, R.i32(gvc))
        gia = g("DeyeModbusGridACurrent") or 0
        gib = g("DeyeModbusGridBCurrent") or 0
        gic = g("DeyeModbusGridCCurrent") or 0
        b.setValues(R.ADDR_METER_GRID_CURRENT_A, R.i32(int(gia * 100)))
        b.setValues(R.ADDR_METER_GRID_CURRENT_B, R.i32(int(gib * 100)))
        b.setValues(R.ADDR_METER_GRID_CURRENT_C, R.i32(int(gic * 100)))
        grid_total = g("DeyeModbusGridTotal")
        if grid_total is None:
            pga = g("DeyeModbusGridAPower") or 0
            pgb = g("DeyeModbusGridBPower") or 0
            pgc = g("DeyeModbusGridCPower") or 0
            grid_total = pga + pgb + pgc
        b.setValues(R.ADDR_METER_ACTIVE_POWER, R.i32(int(grid_total)))

        # mark update success
        self._last_update_ts = time.time()
        self._last_update_ok = True
        self._last_error = None

    def mark_update_failed(self, err: str) -> None:
        self._last_update_ts = time.time()
        self._last_update_ok = False
        self._last_error = err


async def run_modbus_server(emulator: HuaweiModbusEmulator, host: str, port: int) -> None:
    """Avvia il server TCP. Blocca finche' non viene cancellato."""
    log.info("Modbus TCP server starting on %s:%d (unit_id=%d)",
             host, port, emulator._settings.modbus_unit_id)
    await StartAsyncTcpServer(context=emulator.context, address=(host, port))


async def run_openhab_poller(emulator: HuaweiModbusEmulator, oh: OpenHabClient, interval_s: float) -> None:
    """Loop background: ogni interval_s legge OH e aggiorna i registri."""
    log.info("OpenHAB poller starting (interval=%.1fs)", interval_s)
    while True:
        try:
            items = await oh.fetch_all()
            emulator.apply_values(items)
            log.debug("update OK from OH (%d items)", len(items))
        except asyncio.CancelledError:
            log.info("OpenHAB poller stop")
            raise
        except Exception as e:
            log.warning("OH poll failed: %s", e)
            emulator.mark_update_failed(str(e))
        await asyncio.sleep(interval_s)
