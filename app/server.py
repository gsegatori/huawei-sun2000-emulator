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
from app.openhab import MOCK_ITEMS, OpenHabClient

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

    # ───── translation view (cruscotto Live → Mock → Viaris) ─────

    @property
    def last_translation(self) -> dict | None:
        """Ultimo snapshot {input_values, mock_regs, predicted_viaris} per il
        cruscotto di validazione. Popolato da apply_values()."""
        return getattr(self, "_last_translation", None)

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
        # Control registers (47075-47086): defaults sensati. La Viaris
        # all'handshake scrive 47077 = [0, 0] (Max discharging power = 0
        # = no limit). Pre-popolando con 0 evitiamo la write ridondante.
        self._block.setValues(R.ADDR_MAX_CHARGING_POWER, R.u32(0))    # 0 = no limit
        self._block.setValues(R.ADDR_MAX_DISCHARGING_POWER, R.u32(0)) # 0 = no limit
        self._block.setValues(R.ADDR_CHARGING_CUTOFF_SOC, R.u16(1000))  # 100.0% (no limit superior)
        self._block.setValues(R.ADDR_DISCHARGE_CUTOFF_SOC, R.u16(100))  # 10.0% min
        self._block.setValues(R.ADDR_STORAGE_WORKING_MODE, R.u16(2))    # Max self-consumption

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

        # Salviamo le correnti reali Deye per il cruscotto (le scriviamo
        # imbrogliate piu' avanti per pilotare la formula Viaris).
        ia = g("DeyeModbusInverterACurrent") or 0
        ib = g("DeyeModbusInverterBCurrent") or 0
        ic = g("DeyeModbusInverterCCurrent") or 0

        # Active power AC (output inverter, W) — applica IMBROGLIO Viaris.
        # active_total_real = quel che eroga davvero l'inverter (fisico).
        # active_total = (PV + AC_real)/2: cosi' la Viaris calcola
        # Battery_display = 2*(PV - AC_mock) = PV - AC_real = battery_real.
        active_total_real = g("DeyeModbusInverterTotal")
        if active_total_real is None:
            pa = g("DeyeModbusInverterAPower") or 0
            pb = g("DeyeModbusInverterBPower") or 0
            pc = g("DeyeModbusInverterCPower") or 0
            active_total_real = pa + pb + pc
        active_total = ((pv_tot or 0) + (active_total_real or 0)) / 2  # imbroglio
        b.setValues(R.ADDR_ACTIVE_POWER, R.i32(int(active_total)))

        # IMBROGLIO correnti inverter (32072/74/76): distribuisco AC_mock
        # equamente sulle 3 fasi cosi' che sum(V_phase * I_phase) = AC_mock.
        # La Viaris a volte ricava AC_view = sum(V*I) invece di leggere
        # direttamente 32080 -> senza questo imbroglio Battery e Home
        # appaiono raddoppiati (= 2*AC_real invece di AC_mock).
        ac_mock_per_phase = active_total / 3.0
        for addr, v_phase in (
            (R.ADDR_GRID_CURRENT_A, va or 230),
            (R.ADDR_GRID_CURRENT_B, vb or 230),
            (R.ADDR_GRID_CURRENT_C, vc or 230),
        ):
            i_phase_centiA = int((ac_mock_per_phase / v_phase) * 1000) if v_phase > 0 else 0
            b.setValues(addr, R.i32(i_phase_centiA))

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

        # Battery aggregata (37000+) - layout HUAWEI ufficiale
        charge_p = 0
        if self._settings.huawei_has_battery:
            soc = g("DeyeModbusBatterySoc") or 0
            btemp = g("DeyeModbusBatteryTemp") or 25
            b.setValues(R.ADDR_BATTERY_SOC, R.u16(int(soc * 10)))
            b.setValues(R.ADDR_BATTERY_TEMPERATURE, R.i16(int(btemp * 10)))
            # Bilancio AC inverter ibrido REALE (non imbrogliato):
            # P_batt = P_pv - P_inverter_real_out
            # >0 = batteria carica, <0 = batteria scarica.
            charge_p = int((pv_tot or 0) - (active_total_real or 0))
            # 37001 = I32 W signed (spec ufficiale Huawei).
            b.setValues(R.ADDR_BATTERY_CHARGE_DISCHARGE_POWER, R.i32(charge_p))
            # Running status: 0=offline,1=standby,2=running,3=fault,4=sleep.
            # Per battery in scarica/carica usiamo "running" (2); standby per ~0.
            bat_status = 2 if abs(charge_p) > 50 else 1
            b.setValues(R.ADDR_BATTERY_RUNNING_STATUS, R.u16(bat_status))

            # Storage Unit 1 (LUNA2000) layout 37738+ - layout HUAWEI ufficiale.
            b.setValues(R.ADDR_STORAGE_UNIT_1_SOC, R.u16(int(soc * 10)))
            b.setValues(R.ADDR_STORAGE_UNIT_1_RUNNING_STATUS, R.u16(bat_status))
            b.setValues(R.ADDR_STORAGE_UNIT_1_BUS_VOLTAGE, R.u16(7200))   # 720.0 V nominale LUNA2000
            b.setValues(R.ADDR_STORAGE_UNIT_1_BUS_VOLTAGE_ALT, R.u16(7200))  # 37750 -> bus voltage, NON power!
            b.setValues(R.ADDR_STORAGE_UNIT_1_BUS_CURRENT, R.i16(int((charge_p / 720) * 10)))
            b.setValues(R.ADDR_STORAGE_UNIT_1_TEMPERATURE, R.i16(int(btemp * 10)))
            # 37743 = VERO charge/discharge power Unit 1 (I32 W signed).
            b.setValues(R.ADDR_STORAGE_UNIT_1_CHARGE_DISCHARGE_POWER, R.i32(charge_p))

        # Smart meter Huawei (37100+) - layout ufficiale (NON il generico DTSU666).
        # Le correnti stanno a 37107, l'active power totale a 37113.
        b.setValues(R.ADDR_METER_STATUS, R.u16(1))  # 1 = meter normale
        b.setValues(R.ADDR_METER_VOLTAGE_A, R.i32(int(va * 10)))
        b.setValues(R.ADDR_METER_VOLTAGE_B, R.i32(int(vb * 10)))
        b.setValues(R.ADDR_METER_VOLTAGE_C, R.i32(int(vc * 10)))
        # Active power totale a 37113 (I32, W, +import / -export).
        pga = g("DeyeModbusGridAPower") or 0
        pgb = g("DeyeModbusGridBPower") or 0
        pgc = g("DeyeModbusGridCPower") or 0
        grid_total = g("DeyeModbusGridTotal")
        if grid_total is None:
            grid_total = pga + pgb + pgc
        b.setValues(R.ADDR_METER_ACTIVE_POWER, R.i32(int(grid_total)))

        # ────────── IMBROGLIO Rete = Grid_total (non Grid_A_phase) ──────────
        # La Viaris calcola "Rete" come V_A * I_A_phase del meter.
        # Su sistema trifase squilibrato, la fase A puo' esportare anche
        # quando il totale e' import (e viceversa) -> display fuorviante.
        # Imbroglio: scriviamo I_A_signed in modo che V_A * I_A = Grid_total.
        # Cosi' la Viaris mostra Rete = -Grid_total (con segno corretto).
        # Conseguenza positiva: anche "Battery" della Viaris si avvicina al
        # battery_real grazie al bilancio interno (Solar-Home+Rete=-Battery).
        v_a_imbr = va if va > 0 else 230
        i_a_imbroglio_centiA = int((grid_total / v_a_imbr) * 100)
        b.setValues(R.ADDR_METER_CURRENT_A, R.i32(i_a_imbroglio_centiA))
        # Fasi B/C: raw (con segno derivato da power signed)
        gib = g("DeyeModbusGridBCurrent") or 0
        gic = g("DeyeModbusGridCCurrent") or 0
        sign_b = -1 if pgb < 0 else 1
        sign_c = -1 if pgc < 0 else 1
        b.setValues(R.ADDR_METER_CURRENT_B, R.i32(int(gib * sign_b * 100)))
        b.setValues(R.ADDR_METER_CURRENT_C, R.i32(int(gic * sign_c * 100)))
        gia = g("DeyeModbusGridACurrent") or 0  # solo per live view
        b.setValues(R.ADDR_METER_REACTIVE_POWER, R.i32(0))  # no dato OH
        b.setValues(R.ADDR_METER_POWER_FACTOR, R.i16(1000))
        b.setValues(R.ADDR_METER_FREQUENCY, R.i16(5000))

        # Calcola la view del cruscotto: Live Deye → Mock Huawei → Predicted Viaris
        # Grid_A_phase ora e' IMBROGLIATO a Grid_total (=> Rete=-Grid_total).
        grid_a_phase = grid_total  # imbroglio attivo: Viaris vede Grid_total per fase A
        self._last_translation = {
            "live": {
                "pv_total_W": pv_tot,
                "inverter_ac_total_W": active_total_real or 0,
                "grid_total_W_signed": g("DeyeModbusGridTotal") or 0,
                "load_total_W": g("DeyeModbusLoadTotal") or 0,
                "battery_output_W_deye_signed": -(charge_p),  # convenzione Deye: +scarica/-carica
                "battery_soc_pct": g("DeyeModbusBatterySoc") or 0,
                "battery_temp_C": g("DeyeModbusBatteryTemp"),
                "inverter_temp_C": g("DeyeModbusAcTemp"),
                "voltages_phase_V": [va, vb, vc],
                "inverter_currents_A": [g("DeyeModbusInverterACurrent"),
                                        g("DeyeModbusInverterBCurrent"),
                                        g("DeyeModbusInverterCCurrent")],
                "grid_currents_A": [gia, gib, gic],
            },
            "mock_huawei_regs": {
                "32064_input_power_W": int(pv_tot or 0),
                "32080_active_power_W_imbroglio": int(active_total or 0),
                "32080_AC_real_W": int(active_total_real or 0),
                "37001_battery_charge_W_signed": charge_p,
                "37113_meter_active_W_signed": int(g("DeyeModbusGridTotal") or 0),
                "37738_soc_pct_x10": int((g("DeyeModbusBatterySoc") or 0) * 10),
                "_imbroglio_active": True,  # AC_mock = (PV+AC_real)/2 sempre attivo
            },
            "predicted_viaris": {
                # Formule confermate nei round 1-5:
                "Solar_kW": round((pv_tot or 0) / 1000, 2),
                "Battery_kW_signed": round(2 * ((pv_tot or 0) - (active_total or 0)) / 1000, 2),
                "Home_kW": round((2 * (active_total or 0) - (pv_tot or 0) - grid_a_phase) / 1000, 2),
                "Rete_kW_signed": round(-grid_a_phase / 1000, 2),
                "SoC_pct": int(g("DeyeModbusBatterySoc") or 0),
            },
        }
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


async def run_mock_poller(emulator: HuaweiModbusEmulator, interval_s: float) -> None:
    """Loop background: ogni interval_s applica i valori MOCK_ITEMS fissi.
    Usato quando settings.mock_mode = True per debug visivo dell'app client."""
    log.warning("MOCK MODE: poller usa valori fissi (vedi app/openhab.py:MOCK_ITEMS)")
    while True:
        try:
            emulator.apply_values(dict(MOCK_ITEMS))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("mock poll failed: %s", e)
            emulator.mark_update_failed(str(e))
        await asyncio.sleep(interval_s)
