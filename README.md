# huawei-sun2000-emulator

Emula un inverter **Huawei SUN2000-10KTL-M1** ibrido + batteria LUNA2000 via
**Modbus TCP** sulla porta 502, esponendo i dati di un inverter Deye trifase
letti da OpenHAB.

**Caso d'uso reale**: una wallbox **ORBIS Viaris EVVC30A66AE4C** legge
l'inverter di casa come "Main meter" via Modbus TCP, ma il firmware Viaris
supporta nativamente solo Huawei (e poche altre marche). Con questo
emulatore la wallbox vede un Huawei "virtuale" alimentato dai dati Deye
reali — e mostra correttamente Solar, Battery, Casa, Rete, SoC.

## Architettura

```
   ┌──────────────────────┐    REST /rest/items      ┌──────────────────────┐
   │  Inverter Deye reale │                          │ huawei-sun2000-      │
   │  (modbus binding OH) │ ◄───────────────────────│ emulator             │
   │  192.168.0.93:8899   │                          │ (FastAPI + pymodbus) │
   └──────────────────────┘                          └──────────┬───────────┘
                            ┌──────────────────────┐            │
                            │  OpenHAB             │ ◄──────────┘
                            │  192.168.0.200:8080  │
                            └──────────────────────┘            │ Modbus TCP :502
                                                                ▼
                                                ┌──────────────────────────┐
                                                │ Client Huawei            │
                                                │ (Viaris, FusionSolar,    │
                                                │  Home Assistant huawei_solar) │
                                                └──────────────────────────┘
```

Il container Docker:

- avvia un server **Modbus TCP** sulla porta 502 (binding via `CAP_NET_BIND_SERVICE`, non-root)
- ogni `POLL_INTERVAL_S` legge gli items Deye da OpenHAB (REST batch in una sola GET) e popola i registri Huawei applicando un **set di trasformazioni** ("imbroglio") per compensare le formule display del client
- espone:
  - `/dashboard` — **cruscotto real-time** con 3 colonne: dati Deye live | registri Huawei scritti | predizione display Viaris
  - `/healthz` — JSON di status
  - `/admin/recent-requests` — ring buffer ultime 200 PDU Modbus ricevute (per debug)
  - `/admin/registers` — dump decodificato dei registri Huawei

## Lo "imbroglio" Viaris

Reverse-engineering empirico (5 round mock + round live) ha mostrato che la
Viaris **non legge i registri 1:1**, ma calcola valori derivati con
formule specifiche. Per ottenere display corretti l'emulatore deve scrivere
nei registri valori "ingannati":

| Registro | Cosa scrive l'emulatore | Perché |
|---|---|---|
| `32064` Input Power DC | `PV_real` Deye | Display Solar = PV diretto |
| `32080` Active Power AC | **`(PV + AC_inverter)/2`** | Display Battery = `2×(PV-AC_mock)` = battery_real |
| `32072/74/76` Phase currents | **`AC_mock/3/V_phase`** distribuito sulle 3 fasi | la Viaris fa `Σ V×I` come check, devo allinearlo ad AC_mock |
| `37001` Battery aggregata charge | **`clamp(charge_p_real, ±AC_mock)`** | Di notte la Viaris fa `max(32080, \|37001\|)`; clamp neutralizza |
| `37743` Storage Unit 1 charge | **stesso clamp di 37001** | Di notte formula Home = `32080 + \|37743\|`; clamp dimezza per matchare AC_real |
| `37107` Meter current A | **`Grid_total / V_A`** (con segno) | Display Rete = `−V_A × I_A` → mostra `−Grid_total` invece di solo fase A |
| `37109/11` Meter currents B/C | **`Grid_total / 3 / V_phase`** | Sum V×I_meter = Grid_total senza squilibri |
| `37738` Storage SoC | SoC del Deye × 10 | Letto direttamente |

In `app/server.py:apply_values()` c'è tutto il dettaglio.

## Formule display Viaris confermate

Dopo aver applicato gli imbrogli sopra, la Viaris calcola:

**Giorno (PV > 50 W):**
```
Solar     = 32064 / 1000                                 (kW)
Battery   = 2 × (PV − AC_mock) / 1000                    (kW, +carica/-scarica)
Home/Casa = (2 × AC_mock − PV − Grid_A_phase) / 1000     (kW, ≈ Load reale)
Rete      = − Grid_A_phase / 1000                        (kW, +export/-import)
SoC       = 37738 / 10                                   (%)
```

**Notte (PV = 0):**
```
La Viaris cambia formula e usa max(32080, |37001|) per AC_view.
Il doppio clamp |37001| ≤ AC_mock e |37743| ≤ AC_mock garantisce che
Battery e Home restino al valore reale anche di notte.
```

Verifiche live: scarto massimo ~5% tra display Viaris e dati Deye reali,
dovuto principalmente all'asincronia tra polling OH (5s) e refresh Viaris (~22s).

## Mapping registri Huawei principali

| Reg | Tipo | Significato | Valore scritto |
|---|---|---|---|
| `30000-30014` | STR×15 | Model name | `SUN2000-10KTL-M1` |
| `30015-30024` | STR×10 | Serial Number | `HUAWEI_SN` env |
| `30050` | STR×15 | Software version | `V100R001C00SPC120` |
| `30070` | U16 | Model ID | `6` (= SUN2000-10KTL-M1) |
| `30073-30074` | U32 | Rated power (W) | `HUAWEI_RATED_POWER_W` (10000) |
| `32064-32065` | I32 | Input Power DC (W) | imbroglio PV (vedi sopra) |
| `32072-32077` | I32×3 | Phase currents (A×1000) | imbroglio bilanciato su 3 fasi |
| `32080-32081` | I32 | Active Power AC (W) | imbroglio `(PV+AC)/2` |
| `32085` | U16 | Grid frequency (Hz×100) | `5000` (50 Hz) |
| `32086` | U16 | Efficiency (%×100) | `9800` |
| `32089` | U16 | Device status | `0x0200` se inverter attivo |
| `32106-32107` | U32 | Accumulated yield (kWh×100) | da `DeyeModbusProdTotal` (MWh → kWh×100) |
| `32114-32115` | U32 | Daily yield (kWh×100) | da `DeyeModbusProdDaily` |
| `37001-37002` | I32 | Battery charge/discharge (W signed) | imbroglio clamp |
| `37004` | U16 | Battery SoC (%×10) | dal Deye |
| `37100-37121` | misti | Smart meter DTSU666 layout | per-fase + total |
| `37738-37755` | misti | Storage Unit 1 LUNA2000 | SoC, V bus, currents, charge_p clamped |
| `47075-47086` | misti | Control registers (Active Power Control, Storage Working Mode) | persistenti per write-then-read della Viaris |

## Quick start (deploy su mini PC)

```bash
git clone git@github.com:gsegatori/huawei-sun2000-emulator.git ~/huawei-sun2000-emulator
cd ~/huawei-sun2000-emulator
cp .env.example .env
nano .env   # imposta OPENHAB_BASE_URL=http://192.168.0.200:8080 ecc.
./update.sh
```

Endpoint:
- Modbus TCP: `<host>:502` unit_id qualsiasi (replica per tutti)
- Cruscotto: `http://<host>:5050/dashboard`
- Healthcheck: `http://<host>:5050/healthz`

### Test rapido

```bash
# Verifica Modbus rispondere
mbpoll -m tcp -a 1 -r 32081 -c 2 -t 4 <host>   # Active Power (U32 in 2 reg)
mbpoll -m tcp -a 1 -r 37005 -c 1 -t 4 <host>   # SoC

# Verifica cruscotto
curl -s http://<host>:5050/dashboard
```

### MOCK_MODE per debug

Setta `MOCK_MODE=true` in `.env` per usare valori fissi (vedi
`app/openhab.py:MOCK_ITEMS`). Utile per:
- testare la connessione con valori noti
- riprodurre scenari specifici (notte con batteria scarica, giorno con surplus PV, ecc.)
- isolare bug client

## Sviluppo locale

```bash
python3.12 -m venv .venv-test
.venv-test/bin/pip install -e ".[test]"
.venv-test/bin/pytest
```

I test:
- `test_registers.py` — encoder/decoder big-endian (U16/I16/U32/I32/STR)
- `test_mapping.py` — apply_values → registri Modbus (scaling, segno, clamp, imbroglio)
- `test_openhab.py` — parser stati OH (`NULL`/`UNDEF`/unit suffix) + fetch batch
- `test_e2e_modbus.py` — server TCP live + client pymodbus round-trip + multi-unit-id

## Note tecniche

- **Porta 502 privilegiata**: nel Dockerfile `setcap cap_net_bind_service=+ep` sul binario Python; container non-root.
- **pymodbus pinnato `>=3.6,<3.8`**: dalla 3.8 il datastore è stato riscritto (sparse senza `setValues`), break API. La 3.6-3.7 è stabile e funzionante.
- **Datastore continuo**: `ModbusSequentialDataBlock(30000, [0]*20000)` copre 30000-49999 (necessario perché la Viaris fa block-reads di range continui e scrive su `47077`, fuori del range originale `30000-39999`).
- **single=True**: il server risponde a qualunque unit_id (0-247). Alcuni client Viaris/SmartLogger usano slave_id non standard (es. 13).
- **Reactive power, insulation resistance, peak power day**: non presenti in OH, default ragionevoli (0 / 30 MΩ / 0).
- **Voltage PV per stringa (32016/32018)**: hardcoded 400 V (Vmpp tipico LUNA2000). Per fedeltà aggiungere registri Deye specifici (es. reg 676 `Dc voltage 1`).

## Roadmap

- [ ] **Client Deye Modbus diretto** (`app/deye_client.py`): legge direttamente
  `192.168.0.93:8899` invece di passare da OpenHAB. Switch via env
  `DATA_SOURCE=deye|openhab|mock`. Vantaggio: indipendenza da OH, latency
  ~ms invece di ~5s.
- [ ] **Validazione diurna**: ulteriori test con PV>0 reale per confermare
  formule giorno (Round 1-5 già copertura completa).
- [ ] **Documentazione spec Deye**: la mappa completa è nel PDF V102 Deye,
  i registri usati sono in `app/openhab.py:DEYE_ITEMS`.

## Riferimenti

- [wlcrs/huawei-solar-lib v2](https://github.com/wlcrs/huawei-solar-lib) — register map ufficiale Huawei, ground truth
- [olivergregorius/sun2000_modbus](https://github.com/olivergregorius/sun2000_modbus) — cross-reference
- Spec Huawei `SUN2000MA V100R001C00SPC166 Modbus Interface Definitions Issue 08` ([mirror](https://forum.iobroker.net/assets/uploads/files/1732790783983-sun2000ma-v100r001c00spc166-modbus-interface-definitions.pdf))
- Spec Deye `MODBUS RTU 三相储能通信规约 V102` (Three-phase Storage Modbus Protocol V102)
</content>
