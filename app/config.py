from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_file() -> str:
    if override := os.environ.get("HUAWEI_ENV_FILE"):
        if Path(override).is_file():
            return override
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return str(cwd_env)
    repo_env = Path(__file__).resolve().parent.parent / ".env"
    if repo_env.is_file():
        return str(repo_env)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_resolve_env_file(), extra="ignore", case_sensitive=False)

    # Modbus
    modbus_host: str = "0.0.0.0"
    modbus_port: int = 502
    modbus_unit_id: int = 1

    # Admin UI
    admin_host: str = "0.0.0.0"
    admin_port: int = 5050

    # Identity
    huawei_model: str = "SUN2000-10KTL-M1"
    huawei_sn: str = "INV12345678901234"
    huawei_pn: str = "02311GFG"
    huawei_fw: str = "V100R001C00SPC120"
    huawei_rated_power_w: int = 10000
    huawei_max_active_power_w: int = 10000
    huawei_max_apparent_power_va: int = 11000
    huawei_pv_strings: int = 2
    huawei_mppt_count: int = 2
    huawei_has_battery: bool = True

    # OpenHAB
    openhab_base_url: str = "http://192.168.0.200:8080"
    openhab_request_timeout_s: float = 5.0

    poll_interval_s: float = 5.0

    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings()
