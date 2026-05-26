"""Smoke test end-to-end: avvia il server Modbus su porta alta, applica valori,
fa una read da un client pymodbus, verifica che decodifichi correttamente."""
from __future__ import annotations

import asyncio

import pytest
from pymodbus.client import AsyncModbusTcpClient

from app import registers as R
from app.config import Settings
from app.server import HuaweiModbusEmulator, run_modbus_server


@pytest.mark.asyncio
async def test_modbus_server_serves_huawei_registers():
    settings = Settings(
        modbus_host="127.0.0.1",
        modbus_port=15020,
        modbus_unit_id=1,
        openhab_base_url="http://unused",
        huawei_model="SUN2000-10KTL-M1",
        huawei_rated_power_w=10000,
        huawei_sn="TEST00000000000001",
        huawei_has_battery=True,
    )
    emulator = HuaweiModbusEmulator(settings)
    emulator.apply_values({
        "DeyeModbusInverterAVoltage": 235.0,
        "DeyeModbusInverterTotal": 5432,
        "DeyeModbusPvPower": 6000,
        "DeyeModbusProdDaily": 25.5,
        "DeyeModbusBatterySoc": 78,
    })

    server_task = asyncio.create_task(
        run_modbus_server(emulator, "127.0.0.1", 15020)
    )
    try:
        # piccolo settle perche' lo socket si apra
        await asyncio.sleep(0.4)

        client = AsyncModbusTcpClient("127.0.0.1", port=15020)
        connected = await client.connect()
        assert connected, "client failed to connect to emulator"
        try:
            # Model name (15 registers)
            rr = await client.read_holding_registers(R.ADDR_MODEL_NAME, count=15, slave=1)
            assert not rr.isError(), f"read model: {rr}"
            assert R.decode_string(rr.registers) == "SUN2000-10KTL-M1"

            # Rated power (U32)
            rr = await client.read_holding_registers(R.ADDR_RATED_POWER, count=2, slave=1)
            assert not rr.isError()
            assert R.decode_u32(rr.registers) == 10000

            # Grid voltage A
            rr = await client.read_holding_registers(R.ADDR_GRID_VOLTAGE_A, count=1, slave=1)
            assert not rr.isError()
            assert R.decode_u16(rr.registers) == 2350  # 235.0 * 10

            # Active power
            rr = await client.read_holding_registers(R.ADDR_ACTIVE_POWER, count=2, slave=1)
            assert not rr.isError()
            assert R.decode_i32(rr.registers) == 5432

            # Daily yield
            rr = await client.read_holding_registers(R.ADDR_DAILY_YIELD, count=2, slave=1)
            assert not rr.isError()
            assert R.decode_u32(rr.registers) == 2550  # 25.5 * 100

            # Battery SoC
            rr = await client.read_holding_registers(R.ADDR_BATTERY_SOC, count=1, slave=1)
            assert not rr.isError()
            assert R.decode_u16(rr.registers) == 780  # 78% * 10

            # Range continuo 100 reg (come fanno Viaris/SmartLogger/HA) -
            # devono essere TUTTI leggibili, non solo quelli che usiamo.
            rr = await client.read_holding_registers(30000, count=100, slave=1)
            assert not rr.isError(), f"range read 30000-30099 failed: {rr}"
            assert len(rr.registers) == 100

            # Model ID a 30070 deve essere 6 (SUN2000-10KTL-M1)
            rr = await client.read_holding_registers(R.ADDR_MODEL_ID, count=1, slave=1)
            assert not rr.isError()
            assert R.decode_u16(rr.registers) == R.MODEL_ID_SUN2000_10KTL_M1

            # Software version a 30050 deve essere non-zero
            rr = await client.read_holding_registers(R.ADDR_SOFTWARE_VERSION, count=15, slave=1)
            assert not rr.isError()
            assert R.decode_string(rr.registers).startswith("V100R001")
        finally:
            client.close()
    finally:
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, Exception):
            pass
