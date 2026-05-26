# huawei-sun2000-emulator

Emula un inverter **Huawei SUN2000-10KTL-M1** via **Modbus TCP** sulla porta 502,
esponendo i dati di un inverter Deye letti da OpenHAB.

Caso d'uso: integrare l'impianto Deye in ecosistemi che parlano solo dialetto
Huawei (FusionSolar app via cloud, Home Assistant Huawei integration, software
di monitoraggio di terze parti, etc.) senza dover sostituire l'inverter.

## Architettura

```
   ┌──────────────────────┐    REST /rest/items     ┌──────────────────┐
   │  OpenHAB (Deye binding) │ ◄──────────────────  │ huawei-emulator  │
   │  192.168.0.200:8080  │                         │ (FastAPI + pymodbus) │
   └──────────────────────┘                         └────────┬─────────┘
                                                              │ Modbus TCP :502
                                                              ▼
                                                    ┌──────────────────┐
                                                    │ Client Huawei     │
                                                    │ (FusionSolar etc) │
                                                    └──────────────────┘
```

Il container:
- avvia un server **Modbus TCP** su porta 502 (binding via `CAP_NET_BIND_SERVICE`, no root)
- ogni `POLL_INTERVAL_S` legge gli items Deye da OpenHAB (REST batch) e
  aggiorna i registri Huawei (encoding big-endian, scaling corretto)
- espone una **admin UI** su `http://host:5050/` con dump live dei registri

## Mapping registri

I valori dell'inverter Deye vengono mappati nei registri SUN2000 secondo la
specifica Huawei "Solar Inverter Modbus Interface Definitions" v3.x:

| Reg Huawei | Tipo  | Significato         | Sorgente OpenHAB                       |
| ---------- | ----- | ------------------- | -------------------------------------- |
| 30000-30014 | STR  | Modello             | `HUAWEI_MODEL` (default SUN2000-10KTL-M1) |
| 30015-30024 | STR  | Serial Number       | `HUAWEI_SN`                            |
| 30073      | U32   | Rated power (W)     | `HUAWEI_RATED_POWER_W`                 |
| 32064      | I32   | Input power DC (W)  | `DeyeModbusPvPower`                    |
| 32069-32071 | U16  | Grid voltage A/B/C  | `DeyeModbusInverterA/B/CVoltage * 10`  |
| 32072-32077 | I32×3 | Grid current A/B/C  | `DeyeModbusInverterA/B/CCurrent * 1000`|
| 32080      | I32   | Active power AC (W) | `DeyeModbusInverterTotal`              |
| 32085      | U16   | Frequenza (Hz×100)  | fissa 5000 (50 Hz)                     |
| 32087      | I16   | Temperatura interna | `DeyeModbusAcTemp * 10`                |
| 32089      | U16   | Device status       | 0x0200 (running) se PV>50W             |
| 32106      | U32   | Total yield (kWh×100) | `DeyeModbusProdTotal` (MWh→kWh×100)  |
| 32114      | U32   | Daily yield (kWh×100) | `DeyeModbusProdDaily`                |
| 37004      | U16   | Battery SoC (%×10)  | `DeyeModbusBatterySoc * 10`            |
| 37113      | I32   | Smart meter power   | `DeyeModbusGridTotal` (signed: <0 = export) |

Dump completo via `curl http://host:5050/admin/registers`.

## Quick start

```bash
git clone https://github.com/gsegatori/huawei-sun2000-emulator.git
cd huawei-sun2000-emulator
cp .env.example .env
# Edita .env: imposta OPENHAB_BASE_URL e (se vuoi) HUAWEI_SN
./update.sh
```

L'admin UI sara' su http://localhost:5050/

Verifica Modbus TCP da un altro host con `mbpoll`:
```bash
mbpoll -a 1 -r 30001 -c 15 -t 4 192.168.0.x   # legge model (con offset 0-based, alcuni client mostrano reg+1)
mbpoll -a 1 -r 32081 -c 2  -t 4 192.168.0.x   # active power (U32 in 2 reg)
```

## Sviluppo

```bash
python3.12 -m venv .venv-test
.venv-test/bin/pip install -e ".[test]"
.venv-test/bin/pytest
```

I test includono:
- `test_registers.py` — encoder/decoder big-endian (U16/I16/U32/I32/STR)
- `test_mapping.py` — mapping OH items → registri (scaling, signed, defaults)
- `test_openhab.py` — parser stati OH (NULL/UNDEF/unit suffix) + fetch batch
- `test_e2e_modbus.py` — server TCP live + client pymodbus round-trip

## Note

- **Porta 502**: privilegiata, su host serve `setcap` sul binario Python (gia'
  fatto nel Dockerfile). Per dev locale potrebbe servire `sudo` o usare una
  porta alta in `.env` (`MODBUS_PORT=15020`).
- **Reactive power, insulation resistance, peak power day**: non disponibili
  in OH, vengono lasciati a default ragionevoli (0 / 30 MΩ / 0).
- **Tensioni PV per stringa**: il Deye non espone V/I per ogni stringa MPPT
  separatamente nei registri usati; vengono derivate da Pv1Power/Pv2Power
  assumendo Vmpp~400V. Se serve fedelta', aggiungere registri Deye specifici.
- **pymodbus pinnato a <3.8**: dalla 3.8 e' stato riscritto il datastore
  (sparse senza `setValues`); l'API stabile è la 3.6-3.7.
