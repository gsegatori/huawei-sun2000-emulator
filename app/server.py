"""Server Modbus TCP che impersona un Huawei SUN2000.

Architettura:
- ModbusSequentialDataBlock pre-allocato continuo 30000-39999 (10k reg, ~20 KB)
  -> clienti industriali (es. Viaris) chiedono range continui di 100+ reg in
     un'unica request; con sparse + buchi otterebbero IllegalAddress.
- ModbusSlaveContext che espone i registri come Holding Registers (FC=3)
- Update background task che ogni POLL_INTERVAL_S legge OH e fa setValues()
- Server gestito in lifecycle async, condivide il loop con FastAPI admin UI
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import ModbusTcpServer

from app import registers as R
from app.config import Settings
from app.openhab import OpenHabClient

log = logging.getLogger(__name__)

# Ring buffer (process-wide) delle ultime PDU Modbus ricevute. Utile per
# capire cosa client esterni (Viaris, FusionSolar...) leggono in pratica.
RECENT_PDU: deque = deque(maxlen=200)

# Range pre-allocato dei registri Huawei: dal 30000 (device info) fino al
# 49999. Serve coprire ANCHE i registri di controllo a 47xxx (es. 47077 e' un
# control register che la Viaris legge/scrive ciclicamente). 20000 reg
# = ~40 KB di RAM, trascurabile.
_HR_BASE = 30000
_HR_SPAN = 20000


class HuaweiModbusEmulator:
    """Tiene il datastore Modbus, popola i registri identity al boot, espone
    update_from_openhab() per il poller.

    Cosi' separato dal lifecycle del server TCP -> facile da testare in unit
    test senza dover bindare la porta 502.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Blocco continuo 30000..39999 inizializzato a 0. Riempiamo l'identita'
        # subito; i registri runtime vengono popolati dal poller OH.
        self._block = ModbusSequentialDataBlock(_HR_BASE, [0] * _HR_SPAN)
        self._slave = ModbusSlaveContext(hr=self._block, zero_mode=True)
        # single=True -> il server risponde a QUALUNQUE unit_id con lo stesso
        # datastore. Necessario perche' la Viaris e altri client Huawei usano
        # unit_id non standard (0, 1, 13...) a seconda del firmware/modello.
        # MODBUS_UNIT_ID nel .env diventa puramente informativo per /healthz.
        self.context = ModbusServerContext(slaves=self._slave, single=True)
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

    def _populate_identity(self) -> None:
        s = self._settings
        b = self._block
        # Strings device info
        b.setValues(R.ADDR_MODEL_NAME, R.string_regs(s.huawei_model, 15))
        b.setValues(R.ADDR_SN, R.string_regs(s.huawei_sn, 10))
        b.setValues(R.ADDR_PN, R.string_regs(s.huawei_pn, 10))
        b.setValues(R.ADDR_FIRMWARE_VERSION, R.string_regs(s.huawei_fw, 15))
        b.setValues(R.ADDR_SOFTWARE_VERSION, R.string_regs(s.huawei_fw, 15))
        # Versioning / model discovery (la Viaris e altre app si aspettano
        # qui valori non-zero per riconoscere "Huawei").
        b.setValues(R.ADDR_PROTOCOL_VERSION, R.u32(1))
        b.setValues(R.ADDR_MODEL_ID, R.u16(R.MODEL_ID_SUN2000_10KTL_M1))
        b.setValues(R.ADDR_NUMBER_OF_PV_STRINGS, R.u16(s.huawei_pv_strings))
        b.setValues(R.ADDR_NUMBER_OF_MPP_TRACKERS, R.u16(s.huawei_mppt_count))
        b.setValues(R.ADDR_RATED_POWER, R.u32(s.huawei_rated_power_w))
        b.setValues(R.ADDR_MAX_ACTIVE_POWER, R.u32(s.huawei_max_active_power_w))
        b.setValues(R.ADDR_MAX_APPARENT_POWER, R.u32(s.huawei_max_apparent_power_va))
        b.setValues(R.ADDR_MAX_REACTIVE_POWER_FED_TO_GRID, R.i32(0))
        b.setValues(R.ADDR_MAX_REACTIVE_POWER_ABSORBED, R.i32(0))
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

        # Battery aggregata (37000+)
        charge_p = 0
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
            # Storage Unit 1 (LUNA2000) layout 37738+ - letti dalla Viaris.
            b.setValues(R.ADDR_STORAGE_UNIT_1_SOC, R.u16(int(soc * 10)))
            b.setValues(R.ADDR_STORAGE_UNIT_1_RUNNING_STATUS, R.u16(2))  # running
            b.setValues(R.ADDR_STORAGE_UNIT_1_BUS_VOLTAGE, R.u16(7200))  # 720.0 V nominale
            b.setValues(R.ADDR_STORAGE_UNIT_1_BUS_CURRENT, R.i16(int((charge_p / 720) * 10)))
            b.setValues(R.ADDR_STORAGE_UNIT_1_CHARGE_DISCHARGE_POWER, R.i32(charge_p))
            b.setValues(R.ADDR_STORAGE_UNIT_1_TEMPERATURE, R.i16(int(btemp * 10)))

        # Smart meter DTSU666 (37100+) - layout TRIFASE corretto.
        # Tensioni phase-N
        b.setValues(R.ADDR_METER_VOLTAGE_A, R.i32(int(va * 10)))
        b.setValues(R.ADDR_METER_VOLTAGE_B, R.i32(int(vb * 10)))
        b.setValues(R.ADDR_METER_VOLTAGE_C, R.i32(int(vc * 10)))
        # Tensioni line-line (sqrt(3) × media phase-N approssimato)
        b.setValues(R.ADDR_METER_VOLTAGE_AB, R.i32(int(((va + vb) / 2) * 10 * 1.732)))
        b.setValues(R.ADDR_METER_VOLTAGE_BC, R.i32(int(((vb + vc) / 2) * 10 * 1.732)))
        b.setValues(R.ADDR_METER_VOLTAGE_CA, R.i32(int(((vc + va) / 2) * 10 * 1.732)))
        # Correnti per fase (A * 100) - SIGNED, segno = direzione potenza per fase.
        # Sul Deye DeyeModbusGridXCurrent e' magnitudo; deriviamo il segno da
        # DeyeModbusGridXPower (negativo = export, positivo = import).
        gia = g("DeyeModbusGridACurrent") or 0
        gib = g("DeyeModbusGridBCurrent") or 0
        gic = g("DeyeModbusGridCCurrent") or 0
        pga = g("DeyeModbusGridAPower") or 0
        pgb = g("DeyeModbusGridBPower") or 0
        pgc = g("DeyeModbusGridCPower") or 0
        sign_a = -1 if pga < 0 else 1
        sign_b = -1 if pgb < 0 else 1
        sign_c = -1 if pgc < 0 else 1
        b.setValues(R.ADDR_METER_CURRENT_A, R.i32(int(gia * sign_a * 100)))
        b.setValues(R.ADDR_METER_CURRENT_B, R.i32(int(gib * sign_b * 100)))
        b.setValues(R.ADDR_METER_CURRENT_C, R.i32(int(gic * sign_c * 100)))
        # Active power totale a 37119 (NON 37113! quello sono le correnti)
        grid_total = g("DeyeModbusGridTotal")
        if grid_total is None:
            grid_total = pga + pgb + pgc
        b.setValues(R.ADDR_METER_ACTIVE_POWER, R.i32(int(grid_total)))
        # Active power per phase
        b.setValues(R.ADDR_METER_ACTIVE_POWER_A, R.i32(int(pga)))
        b.setValues(R.ADDR_METER_ACTIVE_POWER_B, R.i32(int(pgb)))
        b.setValues(R.ADDR_METER_ACTIVE_POWER_C, R.i32(int(pgc)))

        # mark update success
        self._last_update_ts = time.time()
        self._last_update_ok = True
        self._last_error = None

    def mark_update_failed(self, err: str) -> None:
        self._last_update_ts = time.time()
        self._last_update_ok = False
        self._last_error = err


def _trace_request(request, *client_addr):
    """Logga e accoda ogni request Modbus in arrivo (pymodbus 3.7 API).
    Per le write (FC=6, 15, 16) include anche i values scritti."""
    fc = getattr(request, "function_code", None)
    addr = getattr(request, "address", None)
    cnt = getattr(request, "count", None)
    uid = getattr(request, "slave_id", None)
    # FC=16 (WriteMultipleRegisters): request.values = list[int]
    # FC=6  (WriteSingleRegister):    request.value  = int
    values = getattr(request, "values", None)
    if values is None:
        v = getattr(request, "value", None)
        if v is not None:
            values = [v]
    entry = {
        "ts": time.time(),
        "fc": fc,
        "address": addr,
        "count": cnt,
        "unit_id": uid,
        "values": list(values) if values is not None else None,
        "client": ":".join(str(p) for p in client_addr) if client_addr else None,
    }
    RECENT_PDU.append(entry)
    if values is not None:
        log.info("Modbus WRITE fc=%s addr=%s values=%s unit=%s client=%s",
                 fc, addr, values, uid, entry["client"])
    else:
        log.info("Modbus REQ fc=%s addr=%s count=%s unit=%s client=%s",
                 fc, addr, cnt, uid, entry["client"])


async def run_modbus_server(emulator: HuaweiModbusEmulator, host: str, port: int) -> None:
    """Avvia il server TCP. Blocca finche' non viene cancellato."""
    log.info("Modbus TCP server starting on %s:%d (unit_id=%d, single=True)",
             host, port, emulator._settings.modbus_unit_id)
    server = ModbusTcpServer(
        emulator.context,
        address=(host, port),
        request_tracer=_trace_request,
    )
    await server.serve_forever()


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
