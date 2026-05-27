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
    # correnti inverter A/B/C IMBROGLIATE: I_phase = AC_mock/3/V_phase * 1000
    # AC_mock = (PV+AC_real)/2 = (7042+9996)/2 = 8519, V_A=238.7, V_B=242.1
    # I_A = 8519/3/238.7 * 1000 = 11896
    assert R.decode_i32(e.read_register(R.ADDR_GRID_CURRENT_A, 2)) == 11896
    # active power totale -- ora imbrogliato per Viaris: (PV + AC_real)/2
    # PV=7042, AC_real=9996 -> imbroglio = (7042+9996)/2 = 8519
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
    running status = 1 standby quando |charge_p|<=50W, 2 running altrimenti."""
    e = _make()
    # Caso 1: PV>50 (giorno), batteria carica (+6000 W). Clamp NON applicato.
    e.apply_values({"DeyeModbusPvPower": 8000, "DeyeModbusInverterTotal": 2000})
    assert R.decode_i32(e.read_register(R.ADDR_BATTERY_CHARGE_DISCHARGE_POWER, 2)) == 6000
    assert R.decode_i32(e.read_register(R.ADDR_STORAGE_UNIT_1_CHARGE_DISCHARGE_POWER, 2)) == 6000
    assert R.decode_u16(e.read_register(R.ADDR_BATTERY_RUNNING_STATUS, 1)) == 2
    # Caso 2: PV>50 (giorno con surplus alto), batteria scarica.
    # 545W > 50W, quindi giorno: clamp NON applicato, valore raw -5316.
    e.apply_values({"DeyeModbusPvPower": 545, "DeyeModbusInverterTotal": 5861})
    assert R.decode_i32(e.read_register(R.ADDR_BATTERY_CHARGE_DISCHARGE_POWER, 2)) == -5316
    assert R.decode_i32(e.read_register(R.ADDR_STORAGE_UNIT_1_CHARGE_DISCHARGE_POWER, 2)) == -5316
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
    assert R.decode_i32(e.read_register(R.ADDR_METER_ACTIVE_POWER, 2)) == -3500


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
