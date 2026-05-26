from __future__ import annotations

from app import registers as R


def test_u16_basic():
    assert R.u16(0) == [0]
    assert R.u16(1000) == [1000]
    assert R.u16(0xFFFF) == [0xFFFF]
    assert R.u16(0x12345) == [0x2345]  # truncate


def test_i16_signed():
    assert R.i16(0) == [0]
    assert R.i16(100) == [100]
    assert R.i16(-1) == [0xFFFF]
    assert R.i16(-100) == [0xFFFF - 99]


def test_u32_big_endian():
    assert R.u32(0) == [0, 0]
    assert R.u32(0x12345678) == [0x1234, 0x5678]
    assert R.u32(10000) == [0, 10000]


def test_i32_signed():
    assert R.i32(0) == [0, 0]
    assert R.i32(100) == [0, 100]
    assert R.i32(-1) == [0xFFFF, 0xFFFF]
    assert R.i32(-1000) == [0xFFFF, 0xFFFF - 999]


def test_string_packing():
    assert R.string_regs("AB", 1) == [(ord("A") << 8) | ord("B")]
    assert R.string_regs("ABCD", 2) == [(ord("A") << 8) | ord("B"), (ord("C") << 8) | ord("D")]
    # padding null
    regs = R.string_regs("X", 2)
    assert regs[0] == (ord("X") << 8) | 0
    assert regs[1] == 0


def test_decode_round_trip():
    for v in [0, 100, 10000, 0x7FFFFFFF, 1]:
        assert R.decode_u32(R.u32(v)) == v
    for v in [0, 100, -1, -100, 0x7FFF, -32768]:
        assert R.decode_i16(R.i16(v)) == v
    for v in [0, 100, -1, -100, 2**31 - 1, -(2**31)]:
        assert R.decode_i32(R.i32(v)) == v


def test_decode_string():
    regs = R.string_regs("SUN2000-10KTL-M1", 15)
    assert R.decode_string(regs) == "SUN2000-10KTL-M1"
