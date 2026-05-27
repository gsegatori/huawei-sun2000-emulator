"""Test del mapping OH items -> registri Huawei (scaling + segno)."""
from __future__ import annotations

from app import registers as R
from app.config import Settings
from app.server import HuaweiModbusEmulator


def _make() -> HuaweiModbusEmulator:
    return HuaweiModbusEmulator(Settings(
        modbus_port=15020, admin_port=15050,
        openhab_base_url="http://unused",
        huawei_model="SUN2000-10KTL-M1",
        huawei_rated_power_w=10000,
        huawei_has_battery=True,
    ))


def test_identity_populated_at_boot():
    e = _make()
    model = R.decode_string(e.read_register(R.ADDR_MODEL_NAME, 15))
    assert model == "SUN2000-10KTL-M1"
    rated = R.decode_u32(e.read_register(R.ADDR_RATED_POWER, 2))
    assert rated == 10000
    pv = R.decode_u16(e.read_register(R.ADDR_NUMBER_OF_PV_STRINGS, 1))
    assert pv == 2


def test_apply_voltages_and_currents():
    e = _make()
    e.apply_values({
        "DeyeModbusInverterAVoltage": 238.7,
        "DeyeModbusInverterBVoltage": 242.1,
        "DeyeModbusInverterCVoltage": 231.3,
        "DeyeModbusInverterACurrent": 19.3,
        "DeyeModbusInverterBCurrent": 11.3,
        "DeyeModbusInverterCCurrent": 10.8,
        "DeyeModbusInverterTotal": 9996,
        "DeyeModbusPvPower": 7042,
        "DeyeModbusProdDaily": 41.0,
        "DeyeModbusProdTotal": 45.6129,  # MWh
        "DeyeModbusAcTemp": 35.5,
        "DeyeModbusBatterySoc": 87,
        "DeyeModbusBatteryTemp": 22.5,
    })
    # tensioni in V*10
    assert R.decode_u16(e.read_register(R.ADDR_GRID_VOLTAGE_A, 1)) == int(238.7 * 10)
    assert R.decode_u16(e.read_register(R.ADDR_GRID_VOLTAGE_B, 1)) == int(242.1 * 10)
    # AC_mock = 8519, V_A=238.7. I_A = 8519/3/238.7*1000 = 11896
    assert R.decode_i32(e.read_register(R.ADDR_GRID_CURRENT_A, 2)) == 11896
    # AC_mock = (PV+AC_real)/2 = (7042+9996)/2 = 8519
    assert R.decode_i32(e.read_register(R.ADDR_ACTIVE_POWER, 2)) == 8519
    # PV input
    assert R.decode_i32(e.read_register(R.ADDR_INPUT_POWER, 2)) == 7042
    # temperatura interna °C*10
    assert R.decode_i16(e.read_register(R.ADDR_INTERNAL_TEMPERATURE, 1)) == 355
    # daily energy kWh*100
    assert R.decode_u32(e.read_register(R.ADDR_DAILY_YIELD, 2)) == int(41.0 * 100)
    # accumulated energy: 45.6129 MWh -> 45612.9 kWh -> *100 = 4561290
    assert R.decode_u32(e.read_register(R.ADDR_ACCUMULATED_YIELD, 2)) == 4561290
    # device status: PV>50W -> running
    assert R.decode_u16(e.read_register(R.ADDR_DEVICE_STATUS, 1)) == R.STATUS_ON_GRID_RUNNING
    # battery
    assert R.decode_u16(e.read_register(R.ADDR_BATTERY_SOC, 1)) == 870  # 87%*10
    assert R.decode_i16(e.read_register(R.ADDR_BATTERY_TEMPERATURE, 1)) == 225


def test_apply_handles_missing_values():
    e = _make()
    # Niente items -> tutto defaults
    e.apply_values({})
    # tensione di default 230 V * 10
    assert R.decode_u16(e.read_register(R.ADDR_GRID_VOLTAGE_A, 1)) == 2300
    # active power 0
    assert R.decode_i32(e.read_register(R.ADDR_ACTIVE_POWER, 2)) == 0
    # device status: PV=0 -> standby
    assert R.decode_u16(e.read_register(R.ADDR_DEVICE_STATUS, 1)) == R.STATUS_STANDBY_NO_IRRADIATION


def test_battery_power_i32_signed():
    """37001 e 37743 sono I32 W signed (spec ufficiale Huawei): + carica,
    - scarica. 37750 e' bus voltage U16 V*10 (NON power). 37000/37741
    running status = 1 standby quando |charge_p|<=50W, 2 running altrimenti.
    Viaris firmware aggiornato fa Display Battery = 2 * 37001 -> scriviamo
    charge_p_clamped // 2 cosi' display = charge_p_real."""
    e = _make()
    # Caso 1: batt carica. PV=8000, AC=2000, charge_p=6000, ac_mock=5000.
    # clamp -> 5000, scritto 5000//2 = 2500. Display Viaris: 2*2500=5000 (close to 6000 real).
    e.apply_values({"DeyeModbusPvPower": 8000, "DeyeModbusInverterTotal": 2000})
    assert R.decode_i32(e.read_register(R.ADDR_BATTERY_CHARGE_DISCHARGE_POWER, 2)) == 2500
    assert R.decode_i32(e.read_register(R.ADDR_STORAGE_UNIT_1_CHARGE_DISCHARGE_POWER, 2)) == 2500
    assert R.decode_u16(e.read_register(R.ADDR_BATTERY_RUNNING_STATUS, 1)) == 2
    # Caso 2: batt scarica. PV=545, AC=5861, charge_p=-5316, ac_mock=3203,
    # clamp -> -3203, scritto -3203//2 = -1602 (Python floor div verso -inf).
    # Display Viaris: 2*-1602=-3204 (close to -3203 clamp).
    e.apply_values({"DeyeModbusPvPower": 545, "DeyeModbusInverterTotal": 5861})
    assert R.decode_i32(e.read_register(R.ADDR_BATTERY_CHARGE_DISCHARGE_POWER, 2)) == -1602
    assert R.decode_i32(e.read_register(R.ADDR_STORAGE_UNIT_1_CHARGE_DISCHARGE_POWER, 2)) == -1602
    # Caso 3: standby
    e.apply_values({"DeyeModbusPvPower": 1000, "DeyeModbusInverterTotal": 1010})
    assert R.decode_u16(e.read_register(R.ADDR_BATTERY_RUNNING_STATUS, 1)) == 1
    # 37750 e' bus voltage (NON power): deve essere ~7200 (720 V LUNA2000)
    bus_v = R.decode_u16(e.read_register(R.ADDR_STORAGE_UNIT_1_BUS_VOLTAGE_ALT, 1))
    assert bus_v == 7200


def test_apply_negative_grid_power():
    """Grid power negativo (export) deve essere preservato come signed I32."""
    e = _make()
    e.apply_values({"DeyeModbusGridTotal": -3500})  # esporto 3.5 kW
    # 37113 imbroglio: -Grid_total = +3500 (per fissare Home Viaris)
    assert R.decode_i32(e.read_register(R.ADDR_METER_ACTIVE_POWER, 2)) == 3500


def test_update_marks_last_ok():
    e = _make()
    assert e.last_update_ts == 0
    e.apply_values({"DeyeModbusInverterTotal": 5000})
    assert e.last_update_ts > 0
    assert e.last_update_ok is True


def test_mark_failed():
    e = _make()
    e.mark_update_failed("boom")
    assert e.last_update_ok is False
    assert e.last_error == "boom"
