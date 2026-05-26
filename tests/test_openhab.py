"""Test del client OpenHAB con respx (mock HTTP)."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.openhab import OpenHabClient, parse_number


def test_parse_number_handles_null_states():
    assert parse_number("NULL") is None
    assert parse_number("UNDEF") is None
    assert parse_number(None) is None
    assert parse_number("") is None


def test_parse_number_parses_plain_floats():
    assert parse_number("238.7") == 238.7
    assert parse_number(42) == 42
    assert parse_number(-100.5) == -100.5


def test_parse_number_strips_unit_suffix():
    # OH a volte ritorna "238.7 V" come state
    assert parse_number("238.7 V") == 238.7
    assert parse_number("9996 W") == 9996


def test_parse_number_returns_none_for_garbage():
    assert parse_number("abc") is None
    assert parse_number("  ") is None


@pytest.mark.asyncio
async def test_fetch_all_filters_wanted_items():
    with respx.mock(base_url="http://oh") as m:
        m.get("/rest/items").mock(return_value=httpx.Response(200, json=[
            {"name": "DeyeModbusInverterAVoltage", "state": "238.7 V"},
            {"name": "DeyeModbusInverterTotal", "state": "9996"},
            {"name": "DeyeModbusProdDaily", "state": "41.0"},
            {"name": "DeyeModbusBatterySoc", "state": "NULL"},
            {"name": "OtherUnrelatedItem", "state": "123"},  # va scartato
        ]))
        c = OpenHabClient("http://oh")
        out = await c.fetch_all()
        await c.close()
    assert "OtherUnrelatedItem" not in out
    assert out["DeyeModbusInverterAVoltage"] == 238.7
    assert out["DeyeModbusInverterTotal"] == 9996
    assert out["DeyeModbusBatterySoc"] is None
