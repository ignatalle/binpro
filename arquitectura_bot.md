# Arquitectura técnica — Bot de trading Binomo (ProfitReaper V27 · Versión 5)

Documento de referencia para desarrollo cuantitativo e integración.
Describe el diseño **real y actual** del código en producción, incluyendo todas las optimizaciones introducidas por el **Informe de Estrategia V5**.

*Última actualización: 2 de abril de 2026*

---

## 1. Estructura del proyecto

```
Bot_Binomo/
├── main_bot.py                 # Núcleo: WSS Phoenix, velas, estrategia, órdenes, logs
├── run_volume_flow.py          # Entry alternativo: inyecta strategies.volume_flow
├── test_bot.py                 # Diagnóstico / escucha extendida del WSS
├── test_*.py, debug_*.py       # Utilidades de captura de eventos y diagnóstico
│
├── strategies/
│   ├── volume_flow.py          # ★ Estrategia activa en producción (VFA V5)
│   ├── trend_scalper.py        # EMA, StochRSI, Bollinger
│   ├── breakout.py             # Bandas + EMA + RSI
│   ├── reversion.py            # RSI + Bollinger
│   ├── hyper_scalper.py        # RSI + EMA (scalping)
│   └── auto_hybrid.py          # Orquesta reversion + trend_scalper + breakout
│
├── BinomoAPI/                  # Cliente HTTP/WSS reutilizable
│   ├── api.py                  # Login (requests), balance, modelos
│   ├── wss/
│   │   ├── client.py           # WebSocketClient estándar
│   │   └── enhanced_client.py  # Conexión con múltiples estrategias de auth
│   ├── config/conf.py          # Hosts WSS/HTTP y headers base
│   └── models.py, constants.py, exceptions.py, global_values.py, config_manager.py
│
├── gemini_analysis_log.csv     # ★ ÚNICO log activo — telemetría completa para IA
├── debug_trading.log           # Solo WARNING y ERROR (nivel de producción)
├── informe_estrategia_v5.md    # Análisis cuantitativo base de las reglas V5
└── arquitectura_bot.md         # Este documento
```

### Archivos eliminados en V5

| Archivo (eliminado) | Razón |
|---|---|
| `memory_<RIC>.csv` | Velas migradas a RAM pura (`self.completed_candles`). Sin I/O de disco por vela. |
| `historial_trading.csv` | Redundante con `gemini_analysis_log.csv`. Eliminado para reducir escrituras síncronas. |

---

## 2. Arquitectura "Zero-Latency I/O"

### 2.1 Principio

Antes de V5, cada cierre de vela disparaba dos escrituras síncronas bloqueantes dentro del event loop de asyncio:

```
[cierre de vela]
  ├─► save_candle_to_memory()  → append a memory_*.csv   (~20-100 ms de bloqueo)
  └─► log_to_csv()             → append a historial.csv  (~10-50 ms de bloqueo)
```

En horas pico (H12, H15 UTC) el bot procesaba >2 trades/minuto, con un bloqueo total estimado de 30-150 ms por ciclo, ocupando el 70-90 % del tiempo total en el path crítico.

### 2.2 Solución implementada

Las velas viven **exclusivamente en RAM**:

```python
# ProfitReaperBotV27.__init__
self.completed_candles = []   # lista Python en RAM, máx. 50 velas
```

Al cerrar una vela (`process_tick`), ya **no existe** ninguna operación de disco:

```python
# process_tick() — versión V5
self.completed_candles.append(closed)
if len(self.completed_candles) > 50:
    self.completed_candles.pop(0)
# save_candle_to_memory(closed) ← ELIMINADO
asyncio.create_task(self.check_strategy(closed))
```

`save_candle_to_memory()` y `load_memory()` permanecen en el código como stubs no-op para no romper la interfaz:

```python
def save_candle_to_memory(self, candle):
    pass  # No-op — velas solo en RAM

def load_memory(self):
    return False  # Modo RAM puro, siempre arranca en frío
```

### 2.3 Consecuencias operativas

| Aspecto | Antes (V4) | Ahora (V5) |
|---|---|---|
| Escrituras por vela | 2 archivos CSV | 0 |
| Latencia de I/O en path crítico | 30-150 ms/ciclo | ~0 ms |
| Warm-up al reconectar | Carga desde CSV (hasta 50 velas) | Siempre en frío (~20 min de calentamiento) |
| Persistencia entre reinicios | Sí (hasta 15 min de caducidad) | No |

### 2.4 Único I/O activo en producción

| Archivo | Cuándo escribe | Tipo de operación |
|---|---|---|
| `gemini_analysis_log.csv` | 1 append por vela cerrada + 1 pandas read/write por UUID | CSV síncrono; unavoidable por trazabilidad |
| `debug_trading.log` | Solo niveles `WARNING` y `ERROR` | Escritura excepcional |

El nivel del logger de Python se configuró en `WARNING`:

```python
logging.basicConfig(level=logging.WARNING, format='%(asctime)s | %(message)s')
```

Y `debug_log()` filtra por nivel antes de tocar el disco:

```python
def debug_log(self, message, level="INFO"):
    if DEBUG_MODE and level in ("WARNING", "WARN", "ERROR"):
        # solo aquí se escribe en debug_trading.log
```

---

## 3. Ciclo de vida de la conexión WSS

### 3.1 Arranque

1. `main()` obtiene credenciales, llama `BinomoAPI.login()` y construye `ProfitReaperBotV27` con `authtoken`, `device_id`, `account_type`, `asset_ric`, módulo de estrategia y `timeframe_seconds` (30 o 60).
2. `connect_and_run()`:
   - Llama `load_memory()` → retorna `False` inmediatamente (modo RAM).
   - Abre `async with websockets.connect(uri, extra_headers=headers)` donde `uri = wss://ws.binomo.com/?v=2&vsn=2.0.0`.
   - Headers incluyen `Cookie` (`authtoken`, `device_id`), `authorization-token`, `Origin`, `User-Agent`.
3. Lanza `asyncio.create_task(self.heartbeat_loop())`.
4. Secuencia `phx_join`: `connection` → `bo` (se guarda `bo_join_ref`) → `account` → `user` → `asset:<RIC>` con `rates_required` → `range_stream:<RIC>`.

### 3.2 Heartbeat

`heartbeat_loop` envía cada **25 segundos** un mensaje `{"topic": "phoenix", "event": "heartbeat"}` al servidor. El keep-alive a nivel RFC WebSocket lo gestiona la librería `websockets`.

### 3.3 Reconexión

El `while True` en `main()` envuelve `await bot.connect_and_run()`:

- Errores capturados: `websockets.exceptions.ConnectionClosed`, `ConnectionResetError`, `ConnectionError`, `OSError`, `Exception` genérica.
- Intervalo fijo: `await asyncio.sleep(5)` entre reintentos (sin backoff exponencial).
- En cada reconexión: re-login con `BinomoAPI.login()` para refrescar token.
- `martingale_step` se resetea a 0 en cada nueva instancia del bot.

---

## 4. Flujo de datos (data flow)

### 4.1 Entrada: mensaje WSS → precio y Dark Data

En cada iteración de `async for message in ws`:

1. Se parsea JSON: `topic`, `event`, `payload`.
2. **Canal `bo`** — gestión de operaciones:
   - `phx_reply` con `status: ok` → extrae `uuid` → `on_bo_trade_confirmed_uuid(uuid)`.
   - `closed` → `handle_bo_closed_payload(payload)` → actualiza `resultado` y `profit_real` en `gemini_analysis_log.csv` vía pandas.
3. **Extracción de precio y Dark Data** (primera fuente que aplique):
   - `social_trading_deal`: `entrie_rate` como precio. Si `asset_ric` coincide, el `bet` y `trend` se agregan a `self.volume_memory`.
   - `quotes_range`: `std` → `self.volatility_memory`.
   - `s0` (lista): segundo elemento como precio.
   - `candle` / `s0` (dict): `close` o `price`.

Si `price` quedó definido → `process_tick(float(price))`.

### 4.2 Memoria RAM: velas y Dark Data buffers

| Buffer | Tipo | Contenido | Ventana |
|---|---|---|---|
| `self.completed_candles` | `list` | Velas OHLC cerradas | Últimas 50 |
| `self.current_candle` | `dict` | Vela OHLC en construcción | Vela actual |
| `self.volume_memory` | `list` | `{timestamp, bet, trend, price}` del social feed | Últimos 60 s |
| `self.volatility_memory` | `list` | `{timestamp, std}` de quotes_range | Últimos 60 s |

`clean_old_memory_data()` recorta `volume_memory` y `volatility_memory` a la ventana de 60 s en cada llamada a `check_strategy`.

### 4.3 Construcción de velas y alineación temporal

`process_tick` calcula `time_key` y `candle_start_timestamp`:

- **60 s**: `timestamp // 60 * 60` → clave `HH:MM`.
- **30 s**: `timestamp // 30 * 30` → clave `HH:MM:SS` con `:00` o `:30`.

Al detectar cambio de bucket: cierra la vela anterior, la agrega a `completed_candles` (pop si excede 50), y lanza `asyncio.create_task(check_strategy(closed))`.

### 4.4 Momento exacto de la estrategia

`check_strategy(closed)` es **async** y se dispara como tarea fire-and-forget al **cerrar** cada vela:

1. Verifica `len(completed_candles) >= min_candles` (20 para Volume Flow).
2. Construye `df = pd.DataFrame(self.completed_candles)`.
3. Llama `clean_old_memory_data()`.
4. Calcula RSI(14) con `ta.momentum.RSIIndicator`.
5. Para Volume Flow: `strategy.analyze(df, flow_data=volume_memory, std_data=volatility_memory, rsi=rsi_value, hour=hour_utc)`.
6. Si `action` es `call`/`put` → `await place_trade(...)`.
7. Registra telemetría en `gemini_analysis_log.csv` vía `log_telemetry_for_ai()`.

---

## 5. Estrategia activa: Volume Flow Analysis V5

Módulo: `strategies/volume_flow.py`

### 5.1 Constantes de configuración

```python
LOOKBACK_SECONDS = 15         # Ventana de acumulación de flujo
WHALE_THRESHOLD  = 5_000_000  # Umbral mínimo para detectar una ballena

# Filtros V5 (Informe Estrategia V5 — Reglas P0/P1)
HORAS_PERMITIDAS = {2, 3, 4, 10, 15, 16, 19, 20, 21}  # UTC
WHALE_AMT_MIN    = 28_000_000
WHALE_AMT_MAX    = 999_999_999
STD_MIN          = 8e-8    # Volatilidad mínima para operar
STD_MAX_PUT      = 1.5e-7  # Volatilidad máxima para operar PUT
```

### 5.2 Proceso interno de `analyze()`

La función sigue un pipeline secuencial en 6 fases:

**A. Procesamiento de volumen (Money Flow)**

Itera los eventos recientes de `flow_data` (ventana 15 s) y acumula:
- `call_vol`, `put_vol`
- `money_ratio = call_vol / put_vol` (100.0 si put_vol == 0)
- `whale_detected` y `whale_amount` si algún `bet >= WHALE_THRESHOLD`

**B. Procesamiento de volatilidad (STD)**

Itera `std_data` (ventana 15 s) y calcula:
- `current_std` = último valor STD
- `avg_std` = promedio de la ventana
- `is_volatility_rising = current_std >= avg_std`

Fallback: si `std_data` no aporta valor, calcula `df['close'].pct_change().std()` desde el DataFrame de velas.

**C. Telemetría**

Construye el dict de telemetría unificado (`_telemetry()`) que se transmite al Gemini Logger. Incluye: `ratio`, `call_vol`, `put_vol`, `std_current`, `std_avg`, `whale`, `whale_amount`.

**D. Filtros V5 — Gates en cascada (P0 → P1)**

Los filtros se evalúan en orden estricto. El primero que falla retorna `WAIT` inmediatamente:

```
REGLA 1 (P0) │ datetime.utcnow().hour not in HORAS_PERMITIDAS
             │ → WAIT: "Hora no autorizada"
             ↓
REGLA 4a (P1)│ current_std < STD_MIN  (8e-8)
             │ → WAIT: "Volatilidad muerta"
             ↓
REGLA 2 (P1) │ not is_whale
             │ → WAIT: "Solo operar con trigger whale"
             ↓
REGLA 3 (P0) │ not (28_000_000 ≤ whale_amount ≤ 999_999_999)
             │ → WAIT: "Whale fuera de rango seguro (28M-999M)"
             ↓
RSI          │ CALL con RSI < 45  → WAIT: "RSI bajo para CALL"
             │ PUT con RSI < 45 o RSI > 60  → WAIT: "RSI fuera de zona PUT (45-60)"
             ↓
REGLA 4b (P1)│ whale_detected == 'put' AND current_std > STD_MAX_PUT (1.5e-7)
             │ → WAIT: "Volatilidad excesiva para PUT"
             ↓
SEÑAL        │ → {"action": "call"|"put", "message": "WHALE: Apuesta de X de Y"}
```

**E. Validaciones adicionales** (RSI, ya incluido en el diagrama anterior)

**F. Señal de salida**

Retorna `{"action": whale_detected, "message": "WHALE: Apuesta de CALL/PUT de {amount:.0f}", "telemetry": {...}}`.

### 5.3 Fundamento cuantitativo de los filtros V5

Basado en el análisis de 1 265 operaciones reales (sesión del 1 de abril de 2026):

| Filtro | Base estadística |
|---|---|
| `HORAS_PERMITIDAS` | Horas con WR ≥ 60 % en el dataset. Zonas de la muerte (H17, H18, H22 UTC) tienen WR de 28-36 %. |
| `WHALE_AMT_MIN = 28M` | whale < 28M → WR = 49 % (no rentable). Rango 28-69M → WR = 64 %. |
| `WHALE_AMT_MAX = 999M` | whale ≥ 1B → WR = **25 %** (trampas / manipulación institucional). |
| `STD_MIN = 8e-8` | STD < 8e-8 → WR = 47.6 % (mercado sin movimiento). |
| `STD_MAX_PUT = 1.5e-7` | PUT con STD > 2e-7 → WR = **25 %**. CALL con alta volatilidad → WR = 61.5 % (comportamiento asimétrico). |
| Trigger exclusivo WHALE | Trigger STD/Ratio (no-whale) → WR = 49 % (negativo en expectativa). |

---

## 6. Asincronismo y concurrencia

| Concern | Implementación |
|---|---|
| **Lectura WSS** | Una única corrutina: `async for message in ws` dentro de `connect_and_run`. |
| **Heartbeat** | Tarea en background `heartbeat_loop` vía `create_task`; cada 25 s. Comparte `self.ws`. |
| **Estrategia + trading** | `create_task(check_strategy(closed))` — fire-and-forget al cerrar vela. |
| **I/O activo** | Solo `gemini_analysis_log.csv`: append síncrono por vela + pandas read/write por trade confirmado/cerrado. |
| **Windows** | `asyncio.WindowsSelectorEventLoopPolicy()` antes de `asyncio.run(main())`. |

El modelo es **un solo event loop**, **una** corrutina consumiendo el socket, **tareas fire-and-forget** para análisis y heartbeat concurrente. Las operaciones de disco del Gemini Logger son el único I/O síncrono remanente en el path caliente.

---

## 7. Gemini Logger (telemetría para IA)

### 7.1 Archivo

`gemini_analysis_log.csv` — acumula todas las decisiones (CALL/PUT/WAIT) con contexto completo. Es el único log de datos activo en producción.

### 7.2 Esquema

| Columna | Tipo | Descripción |
|---|---|---|
| `timestamp` | `str` | `YYYY-MM-DD HH:MM:SS.mmm` (milisegundos) |
| `price` | `float` | Precio de cierre de la vela, 8 decimales |
| `decision` | `str` | `CALL`, `PUT` o `WAIT` |
| `std_value` | `float` | Volatilidad STD actual (notación científica) |
| `call_vol_15s` | `float` | Volumen CALL acumulado en los últimos 15 s |
| `put_vol_15s` | `float` | Volumen PUT acumulado en los últimos 15 s |
| `ratio` | `float` | `call_vol / put_vol` |
| `whale_flag` | `bool` | `True` si se detectó una ballena |
| `whale_amount` | `float` | Monto de la ballena (0 si no hay) |
| `strategy_message` | `str` | Razón técnica de la decisión |
| `uuid` | `str` | UUID de la operación en Binomo (`phx_reply → response.uuid`) |
| `resultado` | `str` | `WON` / `LOST` (event `bo/closed`) |
| `profit_real` | `float` | Ganancia neta en USD (`win_centavos / 100`) |

### 7.3 Ciclo de vida de un trade en el CSV

```
check_strategy()
  └─► log_telemetry_for_ai()       → append fila con uuid="" resultado="" profit_real=""
        │
        ↓ (phx_reply llega)
on_bo_trade_confirmed_uuid(uuid)
  └─► assign_uuid_to_latest_trade_row()  → pandas read → rellena uuid → pandas write
        │
        ↓ (bo/closed llega)
update_gemini_log_trade_result(uuid, status, win)
  └─► pandas read → rellena resultado + profit_real → pandas write
```

La cola `self._gemini_uuid_queue` (deque FIFO) maneja el caso en que `phx_reply` llega antes de que la fila sea escrita en el CSV.

### 7.4 Garantía de esquema

`ensure_gemini_csv_schema()` se ejecuta al arrancar e inyecta las columnas `uuid`, `resultado`, `profit_real` si un CSV de sesión anterior no las tiene.

---

## 8. Graceful Shutdown

### 8.1 Problema resuelto

`KeyboardInterrupt` (Ctrl+C) en asyncio provoca que `asyncio.run()` cancele todas las tareas y destruya el event loop **antes** de que el `except` interno pueda ejecutar código que dependa del loop. Intentar hacer cualquier operación async o interactuar con el loop desde ese punto causa `RuntimeError: no running event loop`.

### 8.2 Implementación

El shutdown se maneja en tres niveles:

**Nivel 1 — `except KeyboardInterrupt` en el `while True`:**

```python
except KeyboardInterrupt:
    print("\n\nInterrupcion detectada, cerrando limpiamente...")
    break  # sale del bucle; no llama nada async ni async-dependiente
```

**Nivel 2 — `if __name__ == "__main__"` (punto de entrada):**

```python
if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # silencia el traceback; asyncio ya limpió el loop
    finally:
        # Se ejecuta SIEMPRE: con Ctrl+C, error o cierre normal.
        # El loop ya fue destruido → generar_resumen_salida() es 100 % síncrona.
        print("\n\n🛑 DETENIENDO EL BOT... GENERANDO REPORTE DE SESION 🛑")
        generar_resumen_salida()
```

**Nivel 3 — `generar_resumen_salida()`:**

Función síncrona pura definida a nivel de módulo (no dentro de ninguna clase ni corrutina). Lee `gemini_analysis_log.csv` con pandas, filtra por la fecha de hoy, y muestra en consola:

- Inicio y fin de sesión (timestamps del primer y último trade resuelto).
- Total de operaciones, WON, LOST.
- Win Rate (%) con referencia al breakeven teórico (54.02 %).
- Profit bruto acumulado (suma de `profit_real`).
- Veredicto: `RENTABLE` / `MARGINAL` / `NEGATIVA`.

Manejo de errores interno:
- `PermissionError`: el archivo está abierto en Excel → muestra aviso y retorna sin crashear.
- `FileNotFoundError` / `ValueError` / cualquier excepción: muestra `"Sin datos suficientes para el reporte"` y cierra limpiamente.

### 8.3 Cobertura del `finally`

| Causa de cierre | Reporte generado |
|---|---|
| Ctrl+C del operador | ✅ siempre |
| Error de red no recuperable | ✅ siempre |
| Excepción inesperada en `main()` | ✅ siempre |
| Cierre normal del loop | ✅ siempre |

---

## 9. Gestión de capital (Martingala)

| Parámetro | Variable | Valor por defecto |
|---|---|---|
| Monto base | `self.base_amount` | Configurable en arranque (default `AMOUNT = 2000` centavos = $20) |
| Multiplicador | `self.martingale_multiplier` | 1.0 (desactivada) o valor configurado en arranque |
| Paso actual | `self.martingale_step` | 0 (se resetea en cada reconexión) |

Fórmula: `amount = base_amount × (multiplier ^ step)`.

El monto se convierte a centavos antes de enviarse: `amount_in_cents = trade_amount * 100`.

> **Nota:** La lógica de incremento/reset del `martingale_step` según resultado real está preparada en el código pero desactivada (comentada). La estrategia actualmente opera con monto fijo.

---

## 10. Formato del mensaje `bo/create` (ingeniería inversa)

Campos críticos del payload WSS para ejecutar una operación:

| Campo | Formato | Ejemplo |
|---|---|---|
| `amount` | Entero en **centavos** | `200000` (= $2 000) |
| `created_at` | Timestamp en **milisegundos** (13 dígitos) | `1743600000000` |
| `expire_at` | Timestamp en **segundos** (10 dígitos) | `1743600060` |
| `deal_type` | `"demo"` o `"real"` | según selección del usuario |
| `option_type` | `"turbo"` | fijo |
| `ric` | String del activo | `"Z-CRY/IDX"` |
| `trend` | `"call"` o `"put"` | según señal |
| `ref` | String numérico incremental | `"12"` |
| `join_ref` | Ref capturado del `phx_join` al canal `bo` | `"2"` |

**Dead Zone:** si faltan menos de 30 s para el próximo cierre de vela (60 s), o menos de 10 s (30 s), se salta al cierre siguiente para evitar operar en zona de expiración inminente.

---

## 11. Dependencias

| Librería | Uso |
|---|---|
| `websockets` | Cliente WSS en `main_bot.py` y `BinomoAPI/wss/*.py` |
| `asyncio` (stdlib) | Orquestación de conexión, tareas y reconexión |
| `pandas` | DataFrame de velas en `check_strategy`; lectura/escritura de `gemini_analysis_log.csv` |
| `requests` | Login y HTTP en `BinomoAPI.api` |
| `ta` (Technical Analysis) | `RSIIndicator` en `check_strategy` para validación de señal |
| `math` | Cálculo de `expire_at`, Martingala, validación NaN de RSI |
| `json`, `csv`, `logging`, `time`, `collections.deque` | stdlib: protocolo, persistencia, utilidades |

> No existe `requirements.txt` en la raíz. Se recomienda crearlo con versiones fijadas antes del próximo despliegue.

---

## 12. Notas para el desarrollador Quant

- Las señales se evalúan **al cierre de vela** según `timeframe_seconds`, nunca en cada tick.
- El precio operativo depende principalmente de `social_trading_deal` (`entrie_rate`). Sin flujo social activo, otras fuentes (`s0`, `candle`) actúan como respaldo.
- El canal `bo` es crítico: el `join_ref` del `phx_join` inicial debe coincidir con el del `create`. Se captura en `self.bo_join_ref`.
- Cada reconexión reconstruye estado de Martingala a cero y las velas en RAM se repueblan desde cero (warm-up ~20 minutos).
- El trigger STD/Ratio (no-whale) está implementado en el código pero **nunca se alcanza** en producción porque la Regla 2 (P1) bloquea toda operación sin whale antes de llegar a ese bloque.
- Las operaciones de escritura del Gemini Logger (`assign_uuid_to_latest_trade_row`, `update_gemini_log_trade_result`) son síncronas y bloquean el event loop durante la actualización pandas. Son el único cuello de botella de I/O restante.

---

*Documento generado como vista de arquitectura de solo lectura del repositorio actual; no sustituye al código fuente.*
