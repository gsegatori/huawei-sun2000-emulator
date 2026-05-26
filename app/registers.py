"""Mappa dei registri Huawei SUN2000 + encoder big-endian.

Riferimento: "Solar Inverter Modbus Interface Definitions" (Huawei) v3.x.
Le address sono quelle che il client invia in una richiesta Modbus FC=3
(Read Holding Registers), corrispondono ai numeri citati nella documentazione
Huawei (es. 32080 = Active Power).

Tipi:
  U16 / I16 -> 1 registro (16 bit, unsigned / signed two's complement)
  U32 / I32 -> 2 registri, BIG-ENDIAN (high word per primo, come da convenzione Huawei)
  STR(N)    -> N registri, 2 chars ASCII per registro (big-endian byte order)

Tutti gli encoder ritornano list[int] di valori 16-bit pronti per
ModbusSparseDataBlock.setValues(addr, values).
"""
from __future__ import annotations

from typing import Iterable


# ───────────────────── encoder primitive ─────────────────────

def u16(value: int) -> list[int]:
    """Unsigned 16-bit. value clamp [0, 0xFFFF]."""
    return [int(value) & 0xFFFF]


def i16(value: int) -> list[int]:
    """Signed 16-bit two's complement."""
    v = int(value)
    if v < 0:
        v += 0x10000
    return [v & 0xFFFF]


def u32(value: int) -> list[int]:
    """Unsigned 32-bit big-endian (high word first)."""
    v = int(value) & 0xFFFFFFFF
    return [(v >> 16) & 0xFFFF, v & 0xFFFF]


def i32(value: int) -> list[int]:
    """Signed 32-bit two's complement big-endian."""
    v = int(value)
    if v < 0:
        v += 0x100000000
    v &= 0xFFFFFFFF
    return [(v >> 16) & 0xFFFF, v & 0xFFFF]


def string_regs(s: str, n_regs: int) -> list[int]:
    """ASCII string packed in N registers (2 chars/reg, BE, padded NUL)."""
    s = (s or "")[: n_regs * 2]
    s = s.ljust(n_regs * 2, "\x00")
    return [(ord(s[i * 2]) << 8) | ord(s[i * 2 + 1]) for i in range(n_regs)]


# ───────────────────── decoder per debug / UI ─────────────────────

def decode_u16(regs: Iterable[int]) -> int:
    return int(next(iter(regs))) & 0xFFFF


def decode_i16(regs: Iterable[int]) -> int:
    v = int(next(iter(regs))) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def decode_u32(regs: Iterable[int]) -> int:
    hi, lo = list(regs)[:2]
    return ((int(hi) & 0xFFFF) << 16) | (int(lo) & 0xFFFF)


def decode_i32(regs: Iterable[int]) -> int:
    v = decode_u32(regs)
    return v - 0x100000000 if v & 0x80000000 else v


def decode_string(regs: Iterable[int]) -> str:
    chars = []
    for r in regs:
        chars.append(chr((int(r) >> 8) & 0xFF))
        chars.append(chr(int(r) & 0xFF))
    return "".join(chars).rstrip("\x00 ")


# ───────────────────── definizione registri ─────────────────────
# Tutti i registri Huawei che ci interessa popolare. Mantenere ordinato
# per address per leggibilita'.

# Device info (30000+): valori semi-statici (boot)
# Mappa completa (Huawei spec / huawei_solar lib).
ADDR_MODEL_NAME = 30000          # STR(15) = 30 chars
ADDR_SN = 30015                  # STR(10) = 20 chars
ADDR_PN = 30025                  # STR(10) = 20 chars
ADDR_FIRMWARE_VERSION = 30035    # STR(15) = 30 chars
ADDR_SOFTWARE_VERSION = 30050    # STR(15) = 30 chars
ADDR_PROTOCOL_VERSION = 30068    # U32
ADDR_MODEL_ID = 30070            # U16 - key di discovery Huawei (6 = SUN2000-10KTL-M1)
ADDR_NUMBER_OF_PV_STRINGS = 30071
ADDR_NUMBER_OF_MPP_TRACKERS = 30072
ADDR_RATED_POWER = 30073         # U32 (W)
ADDR_MAX_ACTIVE_POWER = 30075    # U32 (W)
ADDR_MAX_APPARENT_POWER = 30077  # U32 (VA)
ADDR_MAX_REACTIVE_POWER_FED_TO_GRID = 30079  # I32 (Var)
ADDR_MAX_REACTIVE_POWER_ABSORBED = 30081     # I32 (Var)

# Model ID enum (selezione SUN2000-(3KTL-10KTL)-M1 family)
MODEL_ID_SUN2000_3KTL_M1 = 1
MODEL_ID_SUN2000_4KTL_M1 = 2
MODEL_ID_SUN2000_5KTL_M1 = 3
MODEL_ID_SUN2000_6KTL_M1 = 4
MODEL_ID_SUN2000_8KTL_M1 = 5
MODEL_ID_SUN2000_10KTL_M1 = 6

# Realtime (32000+): valori dinamici aggiornati dal poller OH
ADDR_STATE_1 = 32000                 # U16 bitfield
ADDR_STATE_2 = 32002                 # U16
ADDR_STATE_3 = 32003                 # U32 bitfield
ADDR_ALARM_1 = 32008                 # U16
ADDR_ALARM_2 = 32009                 # U16
ADDR_ALARM_3 = 32010                 # U16
ADDR_PV1_VOLTAGE = 32016             # I16 (V * 10)
ADDR_PV1_CURRENT = 32017             # I16 (A * 100)
ADDR_PV2_VOLTAGE = 32018             # I16
ADDR_PV2_CURRENT = 32019             # I16
ADDR_INPUT_POWER = 32064             # I32 (W) - DC input totale
ADDR_GRID_VOLTAGE_AB = 32066         # U16 (V * 10)
ADDR_GRID_VOLTAGE_BC = 32067
ADDR_GRID_VOLTAGE_CA = 32068
ADDR_GRID_VOLTAGE_A = 32069          # U16 (V * 10)
ADDR_GRID_VOLTAGE_B = 32070
ADDR_GRID_VOLTAGE_C = 32071
ADDR_GRID_CURRENT_A = 32072          # I32 (A * 1000)
ADDR_GRID_CURRENT_B = 32074
ADDR_GRID_CURRENT_C = 32076
ADDR_PEAK_ACTIVE_POWER_DAY = 32078   # I32 (W)
ADDR_ACTIVE_POWER = 32080            # I32 (W) - AC output
ADDR_REACTIVE_POWER = 32082          # I32 (VAR)
ADDR_POWER_FACTOR = 32084            # I16 (* 1000)
ADDR_GRID_FREQUENCY = 32085          # U16 (Hz * 100)
ADDR_EFFICIENCY = 32086              # U16 (% * 100)
ADDR_INTERNAL_TEMPERATURE = 32087    # I16 (°C * 10)
ADDR_INSULATION_RESISTANCE = 32088   # U16 (MΩ * 1000)
ADDR_DEVICE_STATUS = 32089           # U16 enum
ADDR_FAULT_CODE = 32090              # U16
ADDR_ACCUMULATED_YIELD = 32106       # U32 (kWh * 100)
ADDR_DAILY_YIELD = 32114             # U32 (kWh * 100)

# Status enum values
STATUS_STANDBY_INITIALIZING = 0x0000
STATUS_STANDBY_DETECTING_IRRADIATION = 0x0001
STATUS_STANDBY_GRID_DETECTING = 0x0002
STATUS_STARTING = 0x0100
STATUS_ON_GRID_RUNNING = 0x0200
STATUS_GRID_CONNECTION_NORMAL = 0x0201
STATUS_OFF_GRID_RUNNING = 0x0300
STATUS_STANDBY_NO_IRRADIATION = 0xA000

# Battery (37000+): info storage aggregata
ADDR_BATTERY_RUNNING_STATUS = 37000      # U16 (0=offline, 1=standby, 2=running, ...)
ADDR_BATTERY_CHARGE_DISCHARGE_POWER = 37001  # I32 (W): >0 charge, <0 discharge
ADDR_BATTERY_SOC = 37004                  # U16 (% * 10)
ADDR_BATTERY_TEMPERATURE = 37022          # I16 (°C * 10)
ADDR_BATTERY_TOTAL_CHARGE = 37066         # U32 (kWh * 100)
ADDR_BATTERY_TOTAL_DISCHARGE = 37068      # U32 (kWh * 100)

# Smart meter Huawei DTSU666 (37100+) - layout TRIFASE STANDARD.
# IMPORTANTE: in DTSU666 le correnti stanno a 37113, NON a 37107 -
# a 37107-37112 ci sono le tensioni phase-N.
ADDR_METER_STATUS = 37100                # U16 (1=normal)
ADDR_METER_VOLTAGE_AB = 37101            # I32 (V * 10) line-line
ADDR_METER_VOLTAGE_BC = 37103            # I32
ADDR_METER_VOLTAGE_CA = 37105            # I32
ADDR_METER_VOLTAGE_A = 37107             # I32 (V * 10) phase-N
ADDR_METER_VOLTAGE_B = 37109             # I32
ADDR_METER_VOLTAGE_C = 37111             # I32
ADDR_METER_CURRENT_A = 37113             # I32 (A * 100) signed
ADDR_METER_CURRENT_B = 37115             # I32
ADDR_METER_CURRENT_C = 37117             # I32
ADDR_METER_ACTIVE_POWER = 37119          # I32 (W) >0 import, <0 export
ADDR_METER_REACTIVE_POWER = 37121        # I32 (VAR)
ADDR_METER_POWER_FACTOR = 37123          # I16 (* 1000)
ADDR_METER_FREQUENCY = 37124             # I16 (Hz * 100)
ADDR_METER_POSITIVE_ACTIVE_ENERGY = 37125  # U32 (kWh * 100) imported from grid
ADDR_METER_REVERSE_ACTIVE_ENERGY = 37127   # U32 (kWh * 100) exported to grid
ADDR_METER_REACTIVE_POWER_Q1Q4 = 37129   # I32 (kVarh * 100)
ADDR_METER_REACTIVE_POWER_Q2Q3 = 37131   # I32 (kVarh * 100)
ADDR_METER_ACTIVE_POWER_A = 37133        # I32 (W) per-phase active power
ADDR_METER_ACTIVE_POWER_B = 37135        # I32
ADDR_METER_ACTIVE_POWER_C = 37137        # I32

# Storage Unit 1 (LUNA2000) - schema HUAWEI 37738+
# La Viaris (e altri client moderni) leggono questi address per la batteria.
ADDR_STORAGE_UNIT_1_SOC = 37738              # U16 (% * 10)
ADDR_STORAGE_UNIT_1_RUNNING_STATUS = 37741   # U16
ADDR_STORAGE_UNIT_1_BUS_VOLTAGE = 37746      # U16 (V * 10)
ADDR_STORAGE_UNIT_1_BUS_CURRENT = 37747      # I16 (A * 10)
ADDR_STORAGE_UNIT_1_CHARGE_DISCHARGE_POWER = 37750  # I32 (W)
ADDR_STORAGE_UNIT_1_TEMPERATURE = 37752      # I16 (°C * 10)
ADDR_STORAGE_UNIT_1_TOTAL_CHARGE = 37753     # U32 (kWh * 100)
ADDR_STORAGE_UNIT_1_TOTAL_DISCHARGE = 37755  # U32 (kWh * 100)
ADDR_STORAGE_UNIT_1_SOH = 37743              # U16 (% * 10) - state of health

# Control registers (47xxx): valori settabili dal client (Viaris, FusionSolar)
# per controllare Active Power Limit, Storage Working Mode, ecc.
ADDR_ACTIVE_POWER_CONTROL_MODE = 47075       # U16 (0=unlimited)
ADDR_ACTIVE_POWER_FIXED_VALUE = 47076        # U32 (W, default = rated power)
ADDR_ACTIVE_POWER_PERCENTAGE_DERATING = 47078  # I16 (% * 10, 1000 = 100%)
ADDR_STORAGE_WORKING_MODE = 47086            # U16 (0=adaptive)
