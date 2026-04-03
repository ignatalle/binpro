import asyncio
import json
import logging
import websockets
import pandas as pd
import csv
import os
import time
import math
import importlib
from collections import deque
from datetime import datetime, timedelta, timezone

try:
    from BinomoAPI.api import BinomoAPI
except ImportError:
    from BinomoAPI import BinomoAPI

# --- ⚙️ CONFIGURACIÓN ---
EXPIRATION = 60
AMOUNT = 2000  # Monto base (se ajustará con Martingala)

# DEBUG
DEBUG_MODE = True
DEBUG_FILE = "debug_trading.log"

# MARTINGALA (Gestión de Capital)
MARTINGALE_STEP = 0  # Contador de pasos consecutivos
MARTINGALE_MULTIPLIER = 1.0  # Multiplicador inicial

logging.basicConfig(level=logging.WARNING, format='%(asctime)s | %(message)s')

class ProfitReaperBotV27:
    def __init__(self, authtoken, device_id, account_type, asset_ric, strategy_module, timeframe_seconds=60):
        self.token = authtoken
        self.device_id = device_id
        self.account_type = account_type  # 'demo' o 'real'
        self.asset_ric = asset_ric  # RIC del activo seleccionado
        self.strategy = strategy_module  # Módulo de estrategia cargado dinámicamente
        self.timeframe_seconds = timeframe_seconds  # Duración de vela en segundos (30 o 60)
        self.ws = None
        self.current_candle = None
        self.completed_candles = []
        self.ref_id = 0
        self.pending_trade = None  # Para almacenar operación pendiente
        self.bo_join_ref = None  # Almacenará el ref del phx_join al canal "bo"
        
        # Martingala (Gestión de Capital)
        self.martingale_step = 0  # Contador de pasos consecutivos
        self.martingale_multiplier = 1.0  # Multiplicador inicial
        self.base_amount = AMOUNT  # Monto base
        
        # === 🔥 DARK DATA CAPTURE: Volume Flow & Volatility ===
        self.volume_memory = []  # Lista de {timestamp, bet, trend, price}
        self.volatility_memory = []  # Lista de {timestamp, std}
        self.max_memory_seconds = 60  # Mantener solo últimos 60 segundos
        
        # === 🧠 GEMINI LOGGER: Telemetría para IA ===
        self.gemini_log_file = "gemini_analysis_log.csv"
        # Cola FIFO: phx_reply puede llegar antes o después de escribir la fila en el CSV
        self._gemini_uuid_queue = deque()
        self.init_gemini_logger()
        self.ensure_gemini_csv_schema()
        
        self.init_debug()
        
    def get_ref(self):
        self.ref_id += 1
        return str(self.ref_id)

    def init_csv(self):
        """Stub — historial_trading.csv y memory_*.csv eliminados (ver Informe V5)."""
        pass

    def init_debug(self):
        """Inicializa archivo de debug"""
        if DEBUG_MODE:
            with open(DEBUG_FILE, 'w', encoding='utf-8') as f:
                f.write(f"=== DEBUG TRADING BOT - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write(f"Estrategia: {self.strategy.__name__ if hasattr(self.strategy, '__name__') else 'Unknown'}\n")
                f.write(f"Asset: {self.asset_ric}\n")
                f.write(f"Account Type: {self.account_type}\n\n")

    def init_gemini_logger(self):
        """Inicializa archivo de telemetría para auditoría por IA (Gemini/GPT)"""
        if not os.path.exists(self.gemini_log_file):
            with open(self.gemini_log_file, mode='w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow([
                    "timestamp",           # Hora exacta con milisegundos
                    "price",              # Precio actual
                    "decision",           # CALL, PUT o WAIT
                    "std_value",          # Volatilidad actual
                    "call_vol_15s",       # Volumen CALL en últimos 15s
                    "put_vol_15s",        # Volumen PUT en últimos 15s
                    "ratio",              # Ratio call/put
                    "whale_flag",         # TRUE si hay ballena detectada
                    "whale_amount",       # Monto de la ballena (0 si no hay)
                    "strategy_message",   # Razón técnica de la estrategia
                    "uuid",               # ID operación Binomo (phx_reply → response.uuid)
                    "resultado",          # WON / LOST (event bo closed)
                    "profit_real",        # Valor API win en USD (win centavos / 100)
                ])
            print(f"🧠 Gemini Logger inicializado: {self.gemini_log_file}")

    GEMINI_EXTRA_COLS = ("uuid", "resultado", "profit_real")

    def ensure_gemini_csv_schema(self):
        """Añade uuid/resultado/profit_real a CSV existente si faltan."""
        path = self.gemini_log_file
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return
        try:
            df = pd.read_csv(path, encoding="utf-8")
            changed = False
            for col in self.GEMINI_EXTRA_COLS:
                if col not in df.columns:
                    df[col] = ""
                    changed = True
            if changed:
                df.to_csv(path, index=False, encoding="utf-8")
        except Exception:
            pass

    def _gemini_prepare_df(self, df):
        for col in self.GEMINI_EXTRA_COLS:
            if col not in df.columns:
                df[col] = ""
        return df

    def assign_uuid_to_latest_trade_row(self, uuid_str):
        """
        Asocia uuid a la primera fila CALL/PUT sin uuid (orden FIFO del CSV).
        Returns True si se actualizó una fila.
        """
        if not uuid_str:
            return False
        path = self.gemini_log_file
        if not os.path.exists(path):
            return False
        try:
            df = self._gemini_prepare_df(pd.read_csv(path, encoding="utf-8"))
            ucol = df["uuid"].astype(str).str.strip()
            mask = df["decision"].isin(["CALL", "PUT"]) & (
                df["uuid"].isna() | ucol.eq("") | ucol.str.lower().eq("nan")
            )
            if not mask.any():
                return False
            idx = df[mask].index[0]
            df.at[idx, "uuid"] = str(uuid_str).strip()
            df.to_csv(path, index=False, encoding="utf-8")
            return True
        except Exception as e:
            self.debug_log(f"assign_uuid_to_latest_trade_row: {e}", "ERROR")
            return False

    def on_bo_trade_confirmed_uuid(self, uuid_str):
        """phx_reply: encolar uuid o pegar a la última fila pendiente."""
        if not uuid_str:
            return
        if not self.assign_uuid_to_latest_trade_row(uuid_str):
            self._gemini_uuid_queue.append(str(uuid_str).strip())

    def update_gemini_log_trade_result(self, uuid_str, status_raw, win_cents):
        """
        Rastreador de resultados: actualiza resultado (WON/LOST) y profit_real (USD)
        en la fila con uuid coincidente.
        """
        path = self.gemini_log_file
        if not uuid_str or not os.path.exists(path):
            return
        try:
            win = float(win_cents) if win_cents is not None else 0.0
            profit_usd = round(win / 100.0, 2)
            st = (status_raw or "").strip().lower()
            resultado = "WON" if st == "won" else "LOST"

            df = self._gemini_prepare_df(pd.read_csv(path, encoding="utf-8"))
            u = str(uuid_str).strip()
            mask = df["uuid"].astype(str).str.strip() == u
            if not mask.any():
                self.debug_log(
                    f"⚠️ closed bo: uuid {u} no encontrado en {path}",
                    "WARN",
                )
                print(f"⚠️ [TRACKER] UUID no encontrado en gemini log: {u}")
                return

            df.loc[mask, "resultado"] = resultado
            df.loc[mask, "profit_real"] = profit_usd
            df.to_csv(path, index=False, encoding="utf-8")

            if resultado == "WON":
                print(
                    f"\n✅ [WIN] Operación GANADA! Profit: +${profit_usd:.2f} | uuid={u}"
                )
            else:
                print(f"\n❌ [LOSS] Operación PERDIDA. | uuid={u}")
        except Exception as e:
            self.debug_log(f"update_gemini_log_trade_result: {e}", "ERROR")

    def handle_bo_closed_payload(self, payload):
        if not isinstance(payload, dict):
            return
        uid = payload.get("uuid")
        status = payload.get("status")
        win = payload.get("win", 0)
        if uid and status:
            self.update_gemini_log_trade_result(uid, status, win)

    def debug_log(self, message, level="INFO"):
        """Escribe en disco SOLO WARNING y ERROR. INFO/DEBUG van a /dev/null."""
        if DEBUG_MODE and level in ("WARNING", "WARN", "ERROR"):
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_msg = f"[{timestamp}] [{level}] {message}\n"
            try:
                with open(DEBUG_FILE, 'a', encoding='utf-8') as f:
                    f.write(log_msg)
            except:
                pass
            print(f"⚠️  [{level}]: {message}")

    def log_to_csv(self, price, rsi, action):
        pass  # historial_trading.csv eliminado — redundante con gemini_analysis_log.csv
    
    def log_telemetry_for_ai(self, price, decision, strategy_message, telemetry_data=None):
        """
        🧠 GEMINI LOGGER: Registra telemetría detallada para auditoría por IA
        
        Este método escribe cada decisión (CALL/PUT/WAIT) en un CSV especializado
        que contiene toda la información contextual necesaria para que una IA
        (Gemini, GPT, Claude) pueda auditar el rendimiento y detectar patrones.
        
        Args:
            price: Precio actual del activo
            decision: 'call', 'put' o None (WAIT)
            strategy_message: Mensaje técnico de la estrategia
            telemetry_data: Dict opcional con datos adicionales de la estrategia
        """
        try:
            # Timestamp con milisegundos
            now = datetime.now()
            timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # Milisegundos
            
            # Decision normalizada
            decision_str = decision.upper() if decision else "WAIT"
            
            # Extraer telemetría de la estrategia (si está disponible)
            if telemetry_data is None:
                telemetry_data = {}
            
            # Valores de telemetría (con conversión segura a float)
            # La estrategia puede retornar 'std' o 'std_current'
            std_value_raw = telemetry_data.get('std_current', telemetry_data.get('std', 0))
            try:
                std_value = float(std_value_raw) if std_value_raw else 0.0
            except (ValueError, TypeError):
                std_value = 0.0
            
            call_vol = telemetry_data.get('call_volume', telemetry_data.get('call_vol', 0))
            put_vol = telemetry_data.get('put_volume', telemetry_data.get('put_vol', 0))
            ratio = telemetry_data.get('ratio', 0)
            whale_flag = telemetry_data.get('whale_detected', telemetry_data.get('whale', False))
            whale_amount = telemetry_data.get('whale_amount', 0)
            
            trade_uuid = ""
            if decision_str in ("CALL", "PUT") and self._gemini_uuid_queue:
                trade_uuid = self._gemini_uuid_queue.popleft()

            # Escribir al CSV
            with open(self.gemini_log_file, mode='a', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow([
                    timestamp_str,
                    f"{price:.8f}",
                    decision_str,
                    f"{std_value:.8e}",  # Notación científica para STD (ahora garantizado float)
                    f"{call_vol:.2f}",
                    f"{put_vol:.2f}",
                    f"{ratio:.4f}",
                    str(whale_flag),
                    f"{whale_amount:.2f}",
                    strategy_message,
                    trade_uuid,
                    "",
                    "",
                ])
        except Exception as e:
            self.debug_log(f"❌ Error en Gemini Logger: {e}", "ERROR")
    
    def clean_old_memory_data(self):
        """
        Limpia datos antiguos de volume_memory y volatility_memory
        para evitar saturar la RAM. Mantiene solo los últimos 60 segundos.
        """
        try:
            current_time = datetime.now().timestamp()
            cutoff_time = current_time - self.max_memory_seconds
            
            # Filtrar datos recientes
            self.volume_memory = [d for d in self.volume_memory if d['timestamp'] >= cutoff_time]
            self.volatility_memory = [d for d in self.volatility_memory if d['timestamp'] >= cutoff_time]
            
            # Debug solo si se eliminaron datos
            vol_count = len(self.volume_memory)
            std_count = len(self.volatility_memory)
            if vol_count > 0 or std_count > 0:
                self.debug_log(
                    f"🧹 Memoria limpiada: {vol_count} deals de volumen, {std_count} muestras de STD",
                    "MEMORY"
                )
        except Exception as e:
            self.debug_log(f"❌ Error limpiando memoria: {e}", "ERROR")
    
    def save_candle_to_memory(self, candle):
        """No-op — velas mantenidas solo en RAM (self.completed_candles). Sin I/O de disco."""
        pass
    
    def load_memory(self):
        """Modo RAM puro — sin persistencia en disco. Siempre inicia con velas vacías."""
        return False

    def calculate_martingale_amount(self):
        """
        Calcula el monto de la operación usando la lógica de Martingala.
        La estrategia externa solo decide la dirección, este método decide el monto.
        
        Returns:
            int: Monto en dólares (se convertirá a centavos en place_trade)
        """
        # Si es el primer paso o hubo éxito, resetear
        # (En una implementación completa, esto se ajustaría según resultados de operaciones anteriores)
        # Por ahora, mantenemos la lógica básica de incremento por pasos
        
        if self.martingale_step == 0:
            amount = self.base_amount
        else:
            # Incrementar monto según multiplicador y paso
            amount = int(self.base_amount * (self.martingale_multiplier ** self.martingale_step))
        
        return amount

    async def connect_and_run(self):
        # === CARGAR MEMORIA PERSISTENTE AL INICIO ===
        print(f"\n{'='*60}")
        print("💾 SISTEMA DE MEMORIA PERSISTENTE")
        print(f"{'='*60}")
        memory_loaded = self.load_memory()
        if not memory_loaded:
            print("   Iniciando sin memoria (primera ejecución o memoria caducada)")
        print(f"{'='*60}\n")
        
        uri = "wss://ws.binomo.com/?v=2&vsn=2.0.0"
        cookie_str = f"authtoken={self.token}; device_id={self.device_id}; device_type=web"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Origin": "https://binomo.com",
            "Cookie": cookie_str,
            "authorization-token": self.token,
            "device-id": self.device_id,
            "device-type": "web"
        }

        print(f"🔌 CONECTANDO V27 (MODO OPORTUNISTA - SOCIAL FEED)...")
        print(f"📊 Estrategia: {self.strategy.__name__ if hasattr(self.strategy, '__name__') else 'Unknown'}")
        print(f"💰 Cuenta: {self.account_type.upper()}")
        print(f"🎯 Asset: {self.asset_ric}")
        
        async with websockets.connect(uri, extra_headers=headers) as ws:
            self.ws = ws
            print("✅ Conectado. Autenticando...")
            asyncio.create_task(self.heartbeat_loop())
            
            # --- PROTOCOLO DE ACCESO ---
            await self.join_channel("connection")
            self.bo_join_ref = await self.join_channel("bo")  # Capturar ref del canal bo
            await self.join_channel("account")
            await self.join_channel("user")
            
            self.debug_log(f"✅ Canal 'bo' unido con join_ref: {self.bo_join_ref}", "CONNECTION")
            
            print(f"🎯 Suscribiendo a {self.asset_ric}...")
            # Solicitamos todo, incluso si s0 falla, el social feed nos salvará
            await self.join_channel(f"asset:{self.asset_ric}", {"rates_required": True})
            await self.join_channel(f"range_stream:{self.asset_ric}", {})

            print("🚀 ESCUCHANDO (Usando 'entrie_rate' del tráfico social)...")

            async for message in ws:
                try:
                    data = json.loads(message)
                    event = data.get('event')
                    payload = data.get('payload')
                    topic = data.get('topic')

                    # --- MANEJO DE RESPUESTAS DE TRADING ---
                    if topic == "bo" and event == "phx_reply":
                        if payload and payload.get('status') == 'ok':
                            self.debug_log(f"✅ OPERACIÓN EJECUTADA EXITOSAMENTE: {payload}", "SUCCESS")
                            print(f"\n✅ OPERACIÓN CONFIRMADA: {payload}")
                            resp = payload.get("response") or {}
                            if isinstance(resp, dict):
                                deal_uuid = resp.get("uuid")
                                if deal_uuid:
                                    self.on_bo_trade_confirmed_uuid(deal_uuid)
                            # Resetear Martingala en caso de éxito (ajustar según lógica de negocio)
                            # self.martingale_step = 0
                        elif payload:
                            self.debug_log(f"❌ ERROR EN OPERACIÓN: {payload}", "ERROR")
                            print(f"\n❌ ERROR EN OPERACIÓN: {payload}")
                            # Incrementar paso de Martingala en caso de error (ajustar según lógica de negocio)
                            # self.martingale_step += 1

                    # --- CIERRE DE OPERACIÓN (rastreador automático P&L en gemini CSV) ---
                    elif topic == "bo" and event == "closed":
                        self.handle_bo_closed_payload(payload)

                    price = None

                    # --- LECTOR DE PRECIOS MULTI-FUENTE ---
                    
                    # FUENTE 1: El hallazgo de tus logs (Social Feed)
                    if event == 'social_trading_deal' and isinstance(payload, dict):
                        # Aquí está el oro que encontraste en el debug
                        # El payload tiene: asset_ric, entrie_rate, etc.
                        price = payload.get('entrie_rate')
                        # Verificar que el asset_ric coincida con el activo monitoreado
                        asset_ric = payload.get('asset_ric')
                        if asset_ric and asset_ric == self.asset_ric:
                            self.debug_log(f"💰 PRECIO desde social_trading_deal: {price} (asset: {asset_ric})", "PRICE")
                            
                            # === 🔥 DARK DATA CAPTURE: Guardar datos de volumen ===
                            bet_amount = payload.get('bet', 0)
                            trend_direction = payload.get('trend', 'unknown')
                            
                            if bet_amount > 0 and trend_direction in ['call', 'put']:
                                volume_data = {
                                    'timestamp': datetime.now().timestamp(),
                                    'bet': bet_amount,
                                    'trend': trend_direction,
                                    'price': price
                                }
                                self.volume_memory.append(volume_data)
                                self.debug_log(
                                    f"📊 VOLUMEN CAPTURADO: ${bet_amount:,.0f} {trend_direction.upper()} @ {price}",
                                    "VOLUME"
                                )
                        elif asset_ric:
                            self.debug_log(f"💰 PRECIO desde social_trading_deal (otro asset {asset_ric}): {price}", "PRICE")
                        else:
                            self.debug_log(f"💰 PRECIO desde social_trading_deal: {price}", "PRICE")
                    
                    # === 🔥 DARK DATA CAPTURE: Capturar volatilidad (quotes_range) ===
                    elif event == 'quotes_range' and isinstance(payload, dict):
                        std_value = payload.get('std')
                        if std_value is not None:
                            try:
                                # Conversión segura a float antes de guardar
                                std_float = float(std_value)
                                volatility_data = {
                                    'timestamp': datetime.now().timestamp(),
                                    'std': std_float
                                }
                                self.volatility_memory.append(volatility_data)
                                self.debug_log(
                                    f"📈 VOLATILIDAD CAPTURADA: STD = {std_float:.8e}",
                                    "VOLATILITY"
                                )
                            except (ValueError, TypeError) as e:
                                # Si la conversión falla, solo loguear sin guardar
                                self.debug_log(
                                    f"⚠️  STD inválido recibido: {std_value} (tipo: {type(std_value)})",
                                    "VOLATILITY"
                                )

                    # FUENTE 2: Ticks Rápidos (Listas) - Por si acaso revive s0
                    elif event == 's0' and isinstance(payload, list) and len(payload) >= 2:
                        try: 
                            price = float(payload[1])
                            self.debug_log(f"💰 PRECIO desde s0 (lista): {price}", "PRICE")
                        except: pass

                    # FUENTE 3: Ticks Lentos / Candle
                    elif event == 'candle':
                        price = payload.get('close')
                        self.debug_log(f"💰 PRECIO desde candle: {price}", "PRICE")
                    elif event == 's0' and isinstance(payload, dict):
                         price = payload.get('close') or payload.get('price')
                         if price:
                             self.debug_log(f"💰 PRECIO desde s0 (dict): {price}", "PRICE")

                    # --- PROCESAMIENTO ---
                    if price:
                        self.process_tick(float(price))
                        
                    # Feedback visual de conexión
                    elif event == "phx_reply" and payload.get('status') == 'ok':
                         if "asset" in topic: 
                             print(f"🔓 CANAL OK: {topic}")
                             self.debug_log(f"Canal conectado: {topic}", "CONNECTION")

                except Exception as e:
                    self.debug_log(f"Error procesando mensaje: {str(e)}", "ERROR")
                    continue

    def process_tick(self, price):
        now = datetime.now()
        current_timestamp = now.timestamp()
        
        # Calcular clave de tiempo según timeframe
        if self.timeframe_seconds == 30:
            # Velas de 30 segundos: HH:MM:SS -> HH:MM:00 o HH:MM:30
            seconds_in_minute = now.second
            if seconds_in_minute < 30:
                candle_second = 0
            else:
                candle_second = 30
            time_key = now.strftime("%H:%M") + f":{candle_second:02d}"
            # Timestamp del inicio de la vela actual (redondeado hacia abajo a múltiplo de 30)
            candle_start_timestamp = int(current_timestamp // 30) * 30
        else:
            # Velas de 1 minuto (comportamiento original)
            time_key = now.strftime("%H:%M")
            candle_start_timestamp = int(current_timestamp // 60) * 60
        
        if self.current_candle is None:
            self.current_candle = {
                'time': time_key,
                'timestamp': candle_start_timestamp,
                'open': price,
                'high': price,
                'low': price,
                'close': price
            }
            print(f"\n💎 ¡PRECIO DETECTADO! {price} | Timeframe: {self.timeframe_seconds}s")
            return

        # LOGICA DE CIERRE DE VELA (detecta cambio de timeframe)
        if self.current_candle['timestamp'] != candle_start_timestamp:
            closed = self.current_candle
            self.completed_candles.append(closed)
            if len(self.completed_candles) > 50:  # Aumentado a 50 para Trend Scalper
                self.completed_candles.pop(0)
            
            print(f"\n🕯️ VELA {closed['time']} CERRADA ({self.timeframe_seconds}s): {closed['close']}")
            asyncio.create_task(self.check_strategy(closed))
            
            self.current_candle = {
                'time': time_key,
                'timestamp': candle_start_timestamp,
                'open': price,
                'high': price,
                'low': price,
                'close': price
            }
        else:
            # Actualización tick a tick dentro de la misma vela
            self.current_candle['close'] = price
            self.current_candle['high'] = max(self.current_candle['high'], price)
            self.current_candle['low'] = min(self.current_candle['low'], price)
            print(f"\r📊 {time_key} ({self.timeframe_seconds}s) | $ {price:.5f}", end="", flush=True)

    async def check_strategy(self, candle):
        """
        Verifica la estrategia usando el módulo cargado dinámicamente.
        Ya no contiene lógica de indicadores técnicos, solo delega a la estrategia.
        """
        # Verificar mínimo de velas (ajustado según estrategia)
        # Trend Scalper y Auto Hybrid requieren 50 velas, Breakout requiere 25, Hyper Scalper y Reversión requieren 20
        strategy_str = str(self.strategy)
        if 'trend_scalper' in strategy_str or 'auto_hybrid' in strategy_str:
            min_candles = 50
        elif 'breakout' in strategy_str:
            min_candles = 25
        elif 'hyper_scalper' in strategy_str:
            min_candles = 20
        else:
            min_candles = 20
        
        if len(self.completed_candles) < min_candles:
            print(f" (Calibrando: {len(self.completed_candles)}/{min_candles})", end="")
            return

        df = pd.DataFrame(self.completed_candles)
        try:
            # === LIMPIAR MEMORIA ANTIGUA ===
            self.clean_old_memory_data()
            
            # RSI (14) sobre velas cerradas: misma fuente que logging y Volume Flow
            rsi_value = 0.0
            try:
                from ta.momentum import RSIIndicator
                rsi_indicator = RSIIndicator(close=df['close'], window=14)
                rsi_raw = rsi_indicator.rsi().iloc[-1]
                rsi_value = float(rsi_raw) if pd.notna(rsi_raw) else 0.0
            except Exception:
                rsi_value = 0.0

            hour_utc = int(datetime.now(timezone.utc).hour)

            # === DELEGAR ANÁLISIS A LA ESTRATEGIA ===
            # Detectar si la estrategia es Volume Flow (necesita datos adicionales)
            strategy_str = str(self.strategy)
            if 'volume_flow' in strategy_str:
                result = self.strategy.analyze(
                    df,
                    flow_data=self.volume_memory,
                    std_data=self.volatility_memory,
                    rsi=rsi_value,
                    hour=hour_utc,
                )
            else:
                # Estrategias tradicionales (solo usan DataFrame)
                result = self.strategy.analyze(df)
            
            action = result.get('action')
            message = result.get('message', 'Sin mensaje')
            telemetry = result.get('telemetry', {})  # Datos adicionales para Gemini Logger
            price = candle['close']
            
            print(f" -> {message}")
            
            # === DEBUG: ANÁLISIS DETALLADO ===
            self.debug_log("=" * 60, "ANALYSIS")
            self.debug_log(f"📊 ANÁLISIS DE SEÑAL:", "ANALYSIS")
            self.debug_log(f"   Precio actual: {price:.8f}", "ANALYSIS")
            self.debug_log(f"   Estrategia: {self.strategy.__name__ if hasattr(self.strategy, '__name__') else 'Unknown'}", "ANALYSIS")
            self.debug_log(f"   Mensaje estrategia: {message}", "ANALYSIS")
            
            # === DECISIÓN CALL/PUT ===
            if action == 'call':
                self.debug_log(f"🚀 DECISIÓN: CALL | {message}", "DECISION")
                print(f"\n🚀 SEÑAL: COMPRA (CALL) | {message}")
                print(f"⚡ EJECUTANDO CALL INMEDIATAMENTE...")
                await self.place_trade("call", price, message)
            elif action == 'put':
                self.debug_log(f"🔥 DECISIÓN: PUT | {message}", "DECISION")
                print(f"\n🔥 SEÑAL: VENTA (PUT) | {message}")
                print(f"⚡ EJECUTANDO PUT INMEDIATAMENTE...")
                await self.place_trade("put", price, message)
            else:
                self.debug_log(f"⏸️  DECISIÓN: WAIT | {message}", "DECISION")
            
            # === 🧠 GEMINI LOGGER: Telemetría para auditoría por IA ===
            self.log_telemetry_for_ai(price, action, message, telemetry)
            
            self.debug_log("=" * 60, "ANALYSIS")
        except Exception as e:
            self.debug_log(f"Error en check_strategy: {str(e)}", "ERROR")
            import traceback
            self.debug_log(traceback.format_exc(), "ERROR")
            pass

    async def place_trade(self, direction, price, reason):
        """Ejecuta la operación real en Binomo con formato corregido según ingeniería inversa"""
        try:
            # ===== CORRECCIÓN 1: created_at en MILISEGUNDOS (13 dígitos) =====
            now = time.time()
            created_at = int(now * 1000)  # Milisegundos
            
            # ===== CORRECCIÓN 2: expire_at alineado al cierre de la vela según timeframe =====
            # Calcular el siguiente cierre de vela según timeframe
            if self.timeframe_seconds == 30:
                # Velas de 30 segundos: alinear a múltiplos de 30 segundos
                next_candle_close = math.ceil(now / 30) * 30
                
                # Dead Zone para 30s: si faltan menos de 10 segundos, saltar a la siguiente vela
                seconds_until_close = next_candle_close - now
                if seconds_until_close < 10:
                    next_candle_close += 30
                    self.debug_log(f"⚠️  DEAD ZONE (30s): Faltan {seconds_until_close:.1f}s (< 10s), saltando a siguiente vela", "TIMING")
                
                expire_at = int(next_candle_close)  # SEGUNDOS
                duration_seconds = next_candle_close - now
            else:
                # Velas de 1 minuto (comportamiento original)
                next_minute = math.ceil(now / 60) * 60
                
                # Si faltan menos de 30 segundos para ese cierre, saltar al minuto subsiguiente (Dead Zone)
                seconds_until_close = next_minute - now
                if seconds_until_close < 30:
                    next_minute += 60
                    self.debug_log(f"⚠️  DEAD ZONE (60s): Faltan {seconds_until_close:.1f}s (< 30s), saltando al siguiente minuto", "TIMING")
                
                expire_at = int(next_minute)  # SEGUNDOS
                duration_seconds = next_minute - now
            
            # ===== CORRECCIÓN 3: ref incremental único =====
            # self.get_ref() ya incrementa self.ref_id correctamente
            current_ref = self.get_ref()
            
            # ===== CORRECCIÓN 4: join_ref debe ser el ref del phx_join inicial =====
            # No puede ser hardcodeado, debe ser el ref capturado al unirse al canal "bo"
            if self.bo_join_ref is None:
                self.debug_log("⚠️  WARNING: bo_join_ref no capturado, usando fallback", "WARNING")
                join_ref_value = "9"  # Fallback solo en caso de error
            else:
                join_ref_value = self.bo_join_ref
            
            # ===== GESTIÓN DE CAPITAL (MARTINGALA) =====
            # La estrategia externa solo decide la dirección, aquí decidimos el monto
            trade_amount = self.calculate_martingale_amount()
            amount_in_cents = trade_amount * 100  # ⚠️ CRÍTICO: amount debe estar en CENTAVOS
            
            payload = {
                "topic": "bo",
                "event": "create",
                "payload": {
                    "amount": amount_in_cents,  # ⚠️ En centavos!
                    "created_at": created_at,
                    "deal_type": self.account_type,  # ✅ Usar self.account_type seleccionado por el usuario
                    "expire_at": expire_at,  # ⚠️ En SEGUNDOS! (10 dígitos)
                    "is_state": False,
                    "option_type": "turbo",
                    "ric": self.asset_ric,  # ✅ Usar self.asset_ric seleccionado por el usuario
                    "tournament_id": None,
                    "trend": direction  # "call" o "put"
                },
                "ref": current_ref,
                "join_ref": join_ref_value  # Usar el ref del phx_join al canal "bo"
            }
            
            # Debug detallado con los valores corregidos
            self.debug_log(f"📤 ENVIANDO OPERACIÓN (FORMATO CORREGIDO):", "TRADE")
            self.debug_log(f"   Dirección: {direction.upper()}", "TRADE")
            self.debug_log(f"   Asset: {self.asset_ric}", "TRADE")
            self.debug_log(f"   Monto: ${trade_amount} (base: ${self.base_amount}, step: {self.martingale_step}, mult: {self.martingale_multiplier})", "TRADE")
            self.debug_log(f"   created_at: {created_at} (13 dígitos - Milisegundos) = {datetime.fromtimestamp(created_at/1000).strftime('%H:%M:%S.%f')[:-3]}", "TRADE")
            self.debug_log(f"   expire_at: {expire_at} (10 dígitos - SEGUNDOS) = {datetime.fromtimestamp(expire_at).strftime('%H:%M:%S')}", "TRADE")
            self.debug_log(f"   amount: {amount_in_cents} centavos = ${trade_amount} USD", "TRADE")
            self.debug_log(f"   Duración real: {duration_seconds:.1f} segundos", "TRADE")
            self.debug_log(f"   ref: {current_ref} (incremental único)", "TRADE")
            self.debug_log(f"   join_ref: {join_ref_value} (capturado del phx_join al canal 'bo')", "TRADE")
            self.debug_log(f"   deal_type: {self.account_type}", "TRADE")
            self.debug_log(f"   Razón señal: {reason}", "TRADE")
            self.debug_log(f"   Payload completo: {json.dumps(payload, indent=2)}", "TRADE")
            
            print(f"\n📤 Enviando operación {direction.upper()} a Binomo (FORMATO CORREGIDO)...")
            print(f"   Asset: {self.asset_ric} | Monto: ${trade_amount} ({amount_in_cents} centavos)")
            print(f"   Timeframe: {self.timeframe_seconds}s")
            print(f"   Created: {datetime.fromtimestamp(created_at/1000).strftime('%H:%M:%S')}")
            print(f"   Expire: {datetime.fromtimestamp(expire_at).strftime('%H:%M:%S')} (Cierre de vela {self.timeframe_seconds}s)")
            print(f"   Duration: {duration_seconds:.1f}s | Ref: {current_ref}")
            print(f"   Account: {self.account_type.upper()}")
            print(f"   ⚠️ CORRECCIONES FINALES: expire_at en segundos, amount en centavos")
            
            await self.ws.send(json.dumps(payload))
            
            self.debug_log(f"✅ Mensaje enviado correctamente con formato de ingeniería inversa", "TRADE")
            print(f"✅ Operación {direction.upper()} enviada con formato correcto")
            
        except Exception as e:
            error_msg = f"❌ ERROR al ejecutar operación: {str(e)}"
            self.debug_log(error_msg, "ERROR")
            print(f"\n{error_msg}")
            raise

    async def join_channel(self, topic, payload={}):
        """Une a un canal y devuelve el ref usado (importante para join_ref en operaciones)"""
        current_ref = self.get_ref()
        msg = {"topic": topic, "event": "phx_join", "payload": payload, "ref": current_ref, "join_ref": current_ref}
        await self.ws.send(json.dumps(msg))
        await asyncio.sleep(0.1)
        return current_ref  # Devolver el ref para uso posterior

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(25)
            try:
                if self.ws: await self.ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": self.get_ref()}))
            except: break


def recommend_strategy(asset_ric):
    """
    Recomienda una estrategia basada en el nombre del activo.
    
    Args:
        asset_ric: String con el RIC del activo (ej. "Z-CRY/IDX", "EURUSD")
    
    Returns:
        tuple: (número_recomendado, nombre_estrategia, mensaje)
    """
    asset_upper = asset_ric.upper()
    
    # Recomendación especial para Z-CRY/IDX: Volume Flow
    if "Z-CRY" in asset_upper or asset_upper == "Z-CRY/IDX":
        return (6, "Volume Flow", "💎 Z-CRY/IDX detectado. Recomendación: [6] Volume Flow (Flujo de dinero real + Ballenas)")
    # Para otros cryptos/índices volátiles: Trend Scalper
    elif "CRY" in asset_upper or "IDX" in asset_upper:
        return (1, "Trend Scalper", "🔥 Activo Volátil detectado. Recomendación: [1] Trend Scalper")
    # Para Forex/estables: Reversión
    else:
        return (2, "Reversión", "⚖️ Activo Forex/Estable detectado. Recomendación: [2] Reversión")


def load_strategy(strategy_number):
    """
    Carga dinámicamente el módulo de estrategia seleccionado.
    
    Args:
        strategy_number: 
            1 para Trend Scalper
            2 para Reversión
            3 para Momentum Breakout
            4 para Auto Hybrid (Modo Automático)
            5 para Hyper Scalper (Modo Ametralladora)
            6 para Volume Flow (Análisis de Flujo de Dinero Real)
    
    Returns:
        module: Módulo de la estrategia cargado
    """
    if strategy_number == 1:
        return importlib.import_module("strategies.trend_scalper")
    elif strategy_number == 2:
        return importlib.import_module("strategies.reversion")
    elif strategy_number == 3:
        return importlib.import_module("strategies.breakout")
    elif strategy_number == 4:
        return importlib.import_module("strategies.auto_hybrid")
    elif strategy_number == 5:
        return importlib.import_module("strategies.hyper_scalper")
    elif strategy_number == 6:
        return importlib.import_module("strategies.volume_flow")
    else:
        raise ValueError(f"Estrategia {strategy_number} no válida (rango válido: 1-6)")


def generar_resumen_salida(gemini_log_path: str = "gemini_analysis_log.csv") -> None:
    """
    Lee gemini_analysis_log.csv y muestra un dashboard de sesión en consola.
    Filtra las operaciones del día actual para aislar la sesión en curso.
    Captura cualquier excepción y cierra limpiamente si no hay datos.
    """
    SEP  = "=" * 62
    SEP2 = "-" * 62
    print("\n" + SEP)
    print("       📊  REPORTE DE SESIÓN — ProfitReaper V27")
    print(SEP)
    try:
        if not os.path.exists(gemini_log_path) or os.path.getsize(gemini_log_path) == 0:
            raise FileNotFoundError("archivo vacío o inexistente")

        try:
            df = pd.read_csv(gemini_log_path, encoding="utf-8")
        except PermissionError:
            print("  Archivo bloqueado por otro proceso (p.ej. Excel).")
            print("  Cierra el archivo y ejecuta el bot nuevamente para ver el reporte.")
            print(SEP + "\n")
            return
        if df.empty:
            raise ValueError("CSV sin filas de datos")

        # --- Filtrar solo la sesión de hoy ---
        today_str = datetime.now().strftime("%Y-%m-%d")
        if "timestamp" in df.columns:
            df = df[df["timestamp"].astype(str).str.startswith(today_str)]

        # --- Solo operaciones resueltas ---
        df_r = df[df["resultado"].isin(["WON", "LOST"])].copy()
        if df_r.empty:
            raise ValueError("sin operaciones resueltas en esta sesión")

        # --- Métricas base ---
        total = len(df_r)
        won   = int((df_r["resultado"] == "WON").sum())
        lost  = int((df_r["resultado"] == "LOST").sum())
        wr    = won / total * 100 if total > 0 else 0.0

        df_r["profit_real"] = pd.to_numeric(df_r["profit_real"], errors="coerce").fillna(0.0)
        pnl_bruto = float(df_r["profit_real"].sum())

        # --- Timestamps de inicio/fin ---
        try:
            ts_ini = str(df_r["timestamp"].iloc[0])
            ts_fin = str(df_r["timestamp"].iloc[-1])
        except Exception:
            ts_ini = ts_fin = "N/D"

        # --- Veredicto dinámico (breakeven = 54.02 % para payout ~85 %) ---
        if wr >= 60.0:
            veredicto = "✅  SESION RENTABLE"
        elif wr >= 54.02:
            veredicto = "⚠️   SESION MARGINAL  (sobre breakeven)"
        else:
            veredicto = "❌  SESION NEGATIVA  (bajo breakeven 54.02 %)"

        print(f"  Inicio  : {ts_ini}")
        print(f"  Fin     : {ts_fin}")
        print(SEP2)
        print(f"  Total operaciones  :  {total:>5d}")
        print(f"  Ganadas  (WON)     :  {won:>5d}")
        print(f"  Perdidas (LOST)    :  {lost:>5d}")
        print(f"  Win Rate           :  {wr:>7.2f} %   (breakeven: 54.02 %)")
        print(SEP2)
        print(f"  Profit bruto (WON) : +$ {pnl_bruto:>10,.2f}")
        print(f"  Veredicto          :  {veredicto}")
        print(SEP + "\n")

    except Exception as exc:
        print(f"  Sin datos suficientes para el reporte ({exc})")
        print(SEP + "\n")


async def main():
    print("=" * 60)
    print("🤖 --- BOT V27: OPORTUNISTA (SOCIAL FEED) ---")
    print("=" * 60)
    print()
    
    # === 1. LOGIN ===
    print("📧 INICIO DE SESIÓN")
    print("-" * 60)
    email = input("Email: ")
    password = input("Password: ")
    
    login = BinomoAPI.login(email, password)
    if not login or not login.authtoken:
        print("❌ Error en el login. Verifica tus credenciales.")
        return
    
    print("✅ Login exitoso\n")
    
    # === 2. SELECTOR DE CUENTA ===
    print("💰 TIPO DE CUENTA")
    print("-" * 60)
    print("[1] Demo")
    print("[2] Real")
    account_choice = input("Selecciona tipo de cuenta [1-2] (default: 1): ").strip()
    
    if account_choice == "2":
        account_type = "real"
    else:
        account_type = "demo"  # Default
    
    print(f"✅ Cuenta seleccionada: {account_type.upper()}\n")
    
    # === 2.1. OBTENER Y MOSTRAR BALANCE ===
    print("💵 BALANCE DE LA CUENTA")
    print("-" * 60)
    try:
        # Crear instancia de API con el tipo de cuenta correcto
        # IMPORTANTE: demo=True significa cuenta demo, demo=False significa cuenta real
        is_demo = (account_type == "demo")
        print(f"🔍 Configurando API para cuenta: {'DEMO' if is_demo else 'REAL'}")
        
        # Crear instancia específica para el tipo de cuenta seleccionado
        api_instance = BinomoAPI.create_from_login(
            login, 
            device_id="1b6290ce761c82f3a97189d35d2ed138", 
            demo=is_demo  # False para real, True para demo
        )
        await api_instance.connect()
        
        # LIMPIAR EL CACHE DE BALANCE para forzar obtener el balance correcto
        if hasattr(api_instance, '_cached_balance'):
            old_cache = api_instance._cached_balance
            api_instance._cached_balance = None
            print(f"   🗑️  Cache limpiado (valor anterior: {old_cache})")
        if hasattr(api_instance, '_cached_balance_timestamp'):
            api_instance._cached_balance_timestamp = 0
        
        # Obtener balance del tipo de cuenta seleccionado
        # Pasar explícitamente el account_type para evitar usar el cache incorrecto
        print(f"🔍 Obteniendo balance de cuenta {account_type.upper()}...")
        
        # Intentar obtener ambos balances para verificar
        try:
            demo_balance = await api_instance.get_balance("demo")
            real_balance = await api_instance.get_balance("real")
            print(f"\n🔍 DEBUG - Todos los balances disponibles:")
            print(f"   Demo: ${demo_balance.amount:,.2f} ({demo_balance.account_type})")
            print(f"   Real: ${real_balance.amount:,.2f} ({real_balance.account_type})")
            print(f"   Seleccionado: {account_type.upper()}")
        except Exception as debug_e:
            print(f"   ⚠️  No se pudieron obtener todos los balances: {debug_e}")
        
        # Obtener el balance del tipo seleccionado
        balance = await api_instance.get_balance(account_type)
        balance_amount = balance.amount
        
        # DEBUG: Mostrar información del balance obtenido
        print(f"\n🔍 DEBUG - Balance final obtenido:")
        print(f"   Amount: ${balance_amount:,.2f}")
        print(f"   Account Type recibido: '{balance.account_type}'")
        print(f"   Account Type esperado: '{account_type}'")
        print(f"   Currency: {balance.currency}")
        print(f"   Coincide: {balance.account_type.lower() == account_type.lower()}")
        
        # Verificar que el balance corresponde al tipo de cuenta correcto
        balance_account_type = balance.account_type.lower() if balance.account_type else ""
        selected_account_type = account_type.lower()
        
        if balance_account_type != selected_account_type:
            print(f"\n⚠️  ADVERTENCIA: Discrepancia detectada!")
            print(f"   Tipo seleccionado: {account_type.upper()}")
            print(f"   Tipo del balance obtenido: {balance.account_type.upper() if balance.account_type else 'N/A'}")
            print(f"   Balance mostrado: ${balance_amount:,.2f}")
            print(f"\n   💡 Intentando obtener balance correcto...")
            
            # Intentar crear una nueva instancia específica para el tipo correcto
            try:
                correct_api = BinomoAPI.create_from_login(
                    login,
                    device_id="1b6290ce761c82f3a97189d35d2ed138",
                    demo=(account_type == "demo")
                )
                await correct_api.connect()
                
                # LIMPIAR EL CACHE antes de obtener el balance
                if hasattr(correct_api, '_cached_balance'):
                    correct_api._cached_balance = None
                if hasattr(correct_api, '_cached_balance_timestamp'):
                    correct_api._cached_balance_timestamp = 0
                
                # Esperar un poco para asegurar que la conexión esté lista
                await asyncio.sleep(0.5)
                
                print(f"   🔍 Re-obteniendo balance de {account_type.upper()}...")
                balance = await correct_api.get_balance(account_type)
                balance_amount = balance.amount
                
                print(f"   🔍 DEBUG - Balance corregido:")
                print(f"      Amount: {balance_amount}")
                print(f"      Account Type: {balance.account_type}")
                
                if balance.account_type.lower() == account_type.lower():
                    print(f"   ✅ Balance corregido obtenido: ${balance_amount:,.2f} ({balance.account_type.upper()})")
                else:
                    print(f"   ⚠️  Aún hay discrepancia. Balance: ${balance_amount:,.2f}, Tipo: {balance.account_type}")
            except Exception as e2:
                print(f"   ⚠️  No se pudo corregir: {e2}")
                import traceback
                traceback.print_exc()
        else:
            print(f"✅ Balance verificado correctamente")
        
        # Verificar una vez más antes de mostrar
        final_account_type = balance.account_type.lower() if balance.account_type else ""
        if final_account_type != account_type.lower():
            print(f"\n❌ ERROR: El balance obtenido NO corresponde al tipo seleccionado!")
            print(f"   Seleccionaste: {account_type.upper()}")
            print(f"   Balance obtenido es de: {balance.account_type.upper()}")
            print(f"   Monto mostrado: ${balance_amount:,.2f}")
            print(f"\n   ⚠️  Esto indica un problema con la API o el cache.")
            print(f"   Por favor, verifica manualmente en la plataforma web.")
        
        print(f"\n💰 Balance disponible ({account_type.upper()}): ${balance_amount:,.2f} {balance.currency}")
        print(f"📊 Tipo de cuenta en respuesta: {balance.account_type.upper()}\n")
    except Exception as e:
        print(f"⚠️  No se pudo obtener el balance: {e}")
        import traceback
        print(f"   Detalles del error:")
        traceback.print_exc()
        balance_amount = None
        print("\n   Continuando sin balance...\n")
    
    # === 2.2. CONFIGURACIÓN DE MONTO BASE ===
    print("💵 CONFIGURACIÓN DE MONTO DE OPERACIÓN")
    print("-" * 60)
    if balance_amount:
        print(f"💰 Balance disponible: ${balance_amount:,.2f}")
        print(f"💡 Recomendación: Usa entre 1% y 5% del balance por operación")
        recommended_min = max(1, int(balance_amount * 0.01))
        recommended_max = max(10, int(balance_amount * 0.05))
        print(f"   Monto sugerido: ${recommended_min} - ${recommended_max}")
    else:
        print("💡 Monto recomendado: $1 - $10 para empezar")
    
    amount_input = input(f"\nIngresa el monto base por operación (default: {AMOUNT}): ").strip()
    try:
        base_amount = int(amount_input) if amount_input else AMOUNT
        if base_amount < 1:
            print("⚠️  Monto mínimo es $1, usando $1")
            base_amount = 1
        if balance_amount and base_amount > balance_amount:
            print(f"⚠️  El monto excede el balance (${balance_amount:,.2f}), usando balance disponible")
            base_amount = int(balance_amount)
    except ValueError:
        print(f"⚠️  Entrada inválida, usando default: ${AMOUNT}")
        base_amount = AMOUNT
    
    print(f"✅ Monto base configurado: ${base_amount}\n")
    
    # === 2.3. CONFIGURACIÓN DE MARTINGALA ===
    print("📈 CONFIGURACIÓN DE MARTINGALA")
    print("-" * 60)
    print("La Martingala es una estrategia de gestión de capital que multiplica")
    print("el monto después de cada pérdida para recuperar las pérdidas anteriores.")
    print()
    
    # Mostrar ejemplo basado en el monto base configurado y balance disponible
    print(f"📊 EJEMPLO DE FUNCIONAMIENTO:")
    if balance_amount:
        print(f"   Balance disponible: ${balance_amount:,.2f}")
    print(f"   Monto base configurado: ${base_amount:,}")
    print(f"   Multiplicador ejemplo: 2.0x")
    print()
    
    # Calcular ejemplo con multiplicador 2.0 (estándar)
    example_multiplier = 2.0
    total_invested = 0
    max_example_steps = 5
    last_operation_amount = 0
    
    for step in range(max_example_steps):
        operation_amount = int(base_amount * (example_multiplier ** step))
        total_invested += operation_amount
        last_operation_amount = operation_amount
        
        if step < max_example_steps - 1:
            next_amount = int(base_amount * (example_multiplier ** (step + 1)))
            if balance_amount and total_invested + next_amount > balance_amount:
                print(f"   Operación {step + 1}: ${operation_amount:,} (pierde) → Próxima: ${next_amount:,} ⚠️ EXCEDE BALANCE")
                print(f"   ⚠️  No tienes suficiente balance (${balance_amount:,.2f}) para continuar")
                break
            else:
                remaining = balance_amount - (total_invested + next_amount) if balance_amount else None
                if remaining is not None and remaining >= 0:
                    print(f"   Operación {step + 1}: ${operation_amount:,} (pierde) → Próxima: ${next_amount:,} | Balance restante: ${remaining:,.2f}")
                else:
                    print(f"   Operación {step + 1}: ${operation_amount:,} (pierde) → Próxima: ${next_amount:,}")
        else:
            print(f"   Operación {step + 1}: ${operation_amount:,} (gana) → Recuperas ${operation_amount:,} + ganancia")
    
    print(f"\n   💰 Total invertido en esta secuencia: ${total_invested:,}")
    if balance_amount:
        remaining_after = balance_amount - total_invested
        if remaining_after >= 0:
            print(f"   💵 Balance restante después: ${remaining_after:,.2f}")
        else:
            print(f"   ⚠️  Esta secuencia excede tu balance por ${abs(remaining_after):,.2f}")
    print(f"   🎯 Si ganas en la última operación: ${last_operation_amount:,} + ganancia")
    print()
    
    if balance_amount:
        if total_invested > balance_amount:
            print(f"⚠️  ADVERTENCIA: El ejemplo excede tu balance disponible (${balance_amount:,.2f})")
            print(f"   Reduce el monto base o el multiplicador para operar de forma segura.")
        else:
            remaining = balance_amount - total_invested
            print(f"💡 Con tu balance (${balance_amount:,.2f}):")
            print(f"   Este ejemplo usaría ${total_invested:,} | Te quedarían ${remaining:,.2f}")
    else:
        print("⚠️  ADVERTENCIA: La Martingala puede ser riesgosa.")
        print("   Asegúrate de tener suficiente capital para múltiples operaciones.")
    print()
    
    if balance_amount:
        # Calcular máximo de operaciones posibles con multiplicador 2x
        max_steps = 0
        temp_amount = base_amount
        temp_total = 0
        while temp_total + temp_amount <= balance_amount and max_steps < 10:
            temp_total += temp_amount
            max_steps += 1
            temp_amount *= 2
        
        print(f"💡 Con tu balance (${balance_amount:,.2f}) y monto base (${base_amount}):")
        print(f"   Puedes hacer hasta {max_steps} operaciones consecutivas con multiplicador 2x")
        if max_steps < 3:
            print(f"   ⚠️  ADVERTENCIA: Balance bajo para Martingala agresiva")
            print(f"   Considera reducir el monto base o usar un multiplicador menor (ej: 1.5x)")
        print()
    
    martingale_choice = input("¿Activar Martingala? [s/n] (default: n): ").strip().lower()
    use_martingale = martingale_choice == 's'
    
    if use_martingale:
        print("\n📊 CONFIGURACIÓN DE MULTIPLICADOR DE MARTINGALA")
        print("-" * 60)
        print("Multiplicador: Cuánto se multiplica el monto después de cada pérdida")
        print()
        print("Ejemplos:")
        print("  - 2.0: Duplica el monto (1x → 2x → 4x → 8x)")
        print("  - 1.5: Incrementa 50% (1x → 1.5x → 2.25x → 3.375x)")
        print("  - 3.0: Triplica el monto (1x → 3x → 9x → 27x) - MUY AGRESIVO")
        print()
        
        multiplier_input = input("Ingresa el multiplicador (default: 2.0): ").strip()
        try:
            martingale_multiplier = float(multiplier_input) if multiplier_input else 2.0
            if martingale_multiplier < 1.0:
                print("⚠️  Multiplicador mínimo es 1.0, usando 2.0")
                martingale_multiplier = 2.0
            if martingale_multiplier > 5.0:
                print("⚠️  Multiplicador muy alto (>5.0), confirmar con 's': ", end="")
                confirm = input().strip().lower()
                if confirm != 's':
                    martingale_multiplier = 2.0
                    print("   Usando multiplicador por defecto: 2.0")
        except ValueError:
            print("⚠️  Entrada inválida, usando default: 2.0")
            martingale_multiplier = 2.0
        
        print(f"\n✅ Martingala ACTIVADA")
        print(f"   Monto base: ${base_amount}")
        print(f"   Multiplicador: {martingale_multiplier}x")
        print()
        print("📊 SIMULACIÓN DE PRIMERAS OPERACIONES:")
        for step in range(5):
            amount = int(base_amount * (martingale_multiplier ** step))
            total_invested = sum(int(base_amount * (martingale_multiplier ** i)) for i in range(step + 1))
            print(f"   Operación {step + 1}: ${amount:,} | Total invertido: ${total_invested:,}")
            if balance_amount and total_invested > balance_amount:
                print(f"   ⚠️  Excede balance disponible")
                break
        print()
    else:
        martingale_multiplier = 1.0
        print("✅ Martingala DESACTIVADA (monto fijo)\n")
    
    # === 3. SELECTOR DE ACTIVO ===
    print("🎯 SELECCIÓN DE ACTIVO")
    print("-" * 60)
    asset_ric = input("Ingresa el RIC del activo (ej. Z-CRY/IDX, EURUSD): ").strip()
    
    if not asset_ric:
        print("⚠️  No se ingresó activo, usando default: Z-CRY/IDX")
        asset_ric = "Z-CRY/IDX"
    
    print(f"✅ Activo seleccionado: {asset_ric}\n")
    
    # === 4. SELECTOR DE TIMEFRAME ===
    print("⏱️  SELECCIÓN DE TIMEFRAME")
    print("-" * 60)
    print("[1] 30 Segundos (Alta frecuencia - Scalping rápido)")
    print("[2] 1 Minuto (Frecuencia estándar - Recomendado)")
    print()
    timeframe_choice = input("Selecciona timeframe [1-2] (default: 2): ").strip()
    
    if timeframe_choice == "1":
        timeframe_seconds = 30
        timeframe_name = "30 Segundos"
        print(f"✅ Timeframe seleccionado: {timeframe_name} (Modo Scalping Ultra-Rápido)\n")
    else:
        timeframe_seconds = 60
        timeframe_name = "1 Minuto"
        print(f"✅ Timeframe seleccionado: {timeframe_name} (Modo Estándar)\n")
    
    # === 5. SELECTOR INTELIGENTE DE ESTRATEGIA ===
    print("📊 SELECCIÓN DE ESTRATEGIA")
    print("-" * 60)
    
    recommended_num, recommended_name, recommendation_msg = recommend_strategy(asset_ric)
    print(recommendation_msg)
    print()
    print("Estrategias disponibles:")
    print("[1] Trend Scalper (Para mercados volátiles/Crypto)")
    print("[2] Reversión (Para mercados laterales/Forex)")
    print("[3] Momentum Breakout (Para explosiones de precio)")
    print("[4] 🤖 MODO AUTOMÁTICO (El bot decide según el mercado)")
    print("[5] 🔫 HYPER SCALPER (Modo Ametralladora - Alta frecuencia)")
    print("[6] 💎 VOLUME FLOW (Flujo de dinero real + Ballenas + IA Logger)")
    print()
    
    strategy_choice = input(f"Selecciona estrategia [1-6] (recomendado: {recommended_num}): ").strip()
    
    try:
        strategy_number = int(strategy_choice) if strategy_choice else recommended_num
        if strategy_number not in [1, 2, 3, 4, 5, 6]:
            print(f"⚠️  Opción inválida, usando recomendación: {recommended_num}")
            strategy_number = recommended_num
    except ValueError:
        print(f"⚠️  Entrada inválida, usando recomendación: {recommended_num}")
        strategy_number = recommended_num
    
    # Cargar estrategia dinámicamente
    try:
        strategy_module = load_strategy(strategy_number)
        
        # Mapear número a nombre
        strategy_names = {
            1: "Trend Scalper",
            2: "Reversión",
            3: "Momentum Breakout",
            4: "🤖 MODO AUTOMÁTICO (Auto Hybrid)",
            5: "🔫 HYPER SCALPER (Modo Ametralladora)",
            6: "💎 VOLUME FLOW (Flujo de Dinero Real + IA)"
        }
        strategy_name = strategy_names.get(strategy_number, "Desconocida")
        print(f"✅ Estrategia cargada: {strategy_name}\n")
        
        # Información adicional para Volume Flow
        if strategy_number == 6:
            print("🔥 CARACTERÍSTICAS DE VOLUME FLOW:")
            print("   ✓ Analiza flujo real de dinero (CALL vs PUT)")
            print("   ✓ Detecta ballenas institucionales (>$5M)")
            print("   ✓ Confirmación con volatilidad algorítmica")
            print("   ✓ Detector de absorción (acumulación oculta)")
            print("   ✓ 🧠 Gemini Logger para auditoría por IA")
            print("   ✓ Genera: gemini_analysis_log.csv")
            print()
    except Exception as e:
        print(f"❌ Error cargando estrategia: {str(e)}")
        return
    
    # === RESUMEN DE CONFIGURACIÓN ===
    print("=" * 60)
    print("📋 RESUMEN DE CONFIGURACIÓN")
    print("=" * 60)
    print(f"Email: {email}")
    print(f"Cuenta: {account_type.upper()}")
    if balance_amount:
        print(f"Balance: ${balance_amount:,.2f}")
    print(f"Asset: {asset_ric}")
    print(f"Timeframe: {timeframe_name} ({timeframe_seconds}s)")
    print(f"Estrategia: {strategy_name}")
    print(f"Monto base: ${base_amount}")
    if use_martingale:
        print(f"Martingala: ACTIVADA (Multiplicador: {martingale_multiplier}x)")
    else:
        print(f"Martingala: DESACTIVADA (Monto fijo)")
    print("=" * 60)
    
    # Mensaje especial para Volume Flow
    if strategy_number == 6:
        print()
        print("🔥" + "=" * 58 + "🔥")
        print("   💎 VOLUME FLOW ANALYSIS - DATOS EN TIEMPO REAL")
        print("🔥" + "=" * 58 + "🔥")
        print()
        print("📊 Datos capturados del WebSocket:")
        print("   ✓ social_trading_deal → Flujo de dinero (bet, trend, price)")
        print("   ✓ quotes_range → Volatilidad algorítmica (std)")
        print()
        print("🎯 Tipos de señales:")
        print("   📈 Money Flow: Desequilibrio CALL/PUT (ratio > 2.5 o < 0.4)")
        print("   🐋 Whale: Apuestas institucionales > $5M")
        print("   💎 Absorption: Alto volumen + precio estático → Breakout")
        print()
        print("🧠 Archivos generados:")
        print("   ✓ gemini_analysis_log.csv → Telemetría para IA (único log activo)")
        print("   ✓ debug_trading.log → Solo WARNING y ERROR")
        print()
        print("⚡ Memoria auto-limpiada cada 60 segundos (previene saturación)")
        print("🔥" + "=" * 58 + "🔥")
    
    print()
    
    # Guardar configuración para reconexión
    config = {
        'email': email,
        'password': password,
        'account_type': account_type,
        'asset_ric': asset_ric,
        'timeframe_seconds': timeframe_seconds,
        'timeframe_name': timeframe_name,
        'strategy_number': strategy_number,
        'strategy_name': strategy_name,
        'strategy_module': strategy_module,
        'base_amount': base_amount,
        'martingale_multiplier': martingale_multiplier if use_martingale else 1.0,
        'use_martingale': use_martingale
    }
    
    # Guardar login inicial para primera conexión
    initial_login = login
    
    # === BUCLE DE RECONEXIÓN INFINITO ===
    print("🔄 Modo de reconexión automática activado")
    print("   El bot se reconectará automáticamente en caso de desconexión")
    print("   Presiona Ctrl+C para detener el bot\n")
    
    reconnect_count = 0
    
    while True:
        try:
            # === RE-LOGIN EN CADA RECONEXIÓN ===
            if reconnect_count > 0:
                print(f"\n{'='*60}")
                print(f"🔄 INTENTO DE RECONEXIÓN #{reconnect_count}")
                print(f"{'='*60}")
                print("🔐 Re-autenticando...")
                
                login = BinomoAPI.login(config['email'], config['password'])
                if not login or not login.authtoken:
                    print("❌ Error en el re-login. Verifica tus credenciales.")
                    print("⏳ Reintentando en 5 segundos...")
                    await asyncio.sleep(5)
                    reconnect_count += 1
                    continue
                
                print("✅ Re-login exitoso\n")
            else:
                # Primera conexión, usar login ya obtenido
                login = initial_login
            
            # === INICIALIZAR BOT ===
            bot = ProfitReaperBotV27(
                authtoken=login.authtoken,
                device_id="1b6290ce761c82f3a97189d35d2ed138",
                account_type=config['account_type'],
                asset_ric=config['asset_ric'],
                strategy_module=config['strategy_module'],
                timeframe_seconds=config['timeframe_seconds']
            )
            
            # Configurar monto base y martingala desde la configuración
            bot.base_amount = config.get('base_amount', AMOUNT)
            bot.martingale_multiplier = config.get('martingale_multiplier', 1.0)
            bot.martingale_step = 0  # Resetear paso de martingala al reconectar
            
            # === CONECTAR Y EJECUTAR ===
            await bot.connect_and_run()
            
        except KeyboardInterrupt:
            print("\n\nInterrupcion detectada, cerrando limpiamente...")
            break
            
        except (ConnectionResetError, ConnectionError, OSError) as e:
            # Errores de conexión de red
            reconnect_count += 1
            error_type = type(e).__name__
            print(f"\n{'='*60}")
            print(f"⚠️  DESCONEXIÓN DETECTADA ({error_type})")
            print(f"{'='*60}")
            print(f"❌ Error: {str(e)}")
            print(f"🔄 Reiniciando en 5 segundos... (Intento #{reconnect_count})")
            print(f"{'='*60}\n")
            await asyncio.sleep(5)
            continue
            
        except websockets.exceptions.ConnectionClosed as e:
            # Error específico de WebSocket cerrado
            reconnect_count += 1
            print(f"\n{'='*60}")
            print(f"⚠️  WEBSOCKET CERRADO")
            print(f"{'='*60}")
            print(f"❌ Código: {e.code}, Razón: {e.reason}")
            print(f"🔄 Reiniciando en 5 segundos... (Intento #{reconnect_count})")
            print(f"{'='*60}\n")
            await asyncio.sleep(5)
            continue
            
        except Exception as e:
            # Cualquier otra excepción
            reconnect_count += 1
            error_type = type(e).__name__
            print(f"\n{'='*60}")
            print(f"⚠️  ERROR INESPERADO ({error_type})")
            print(f"{'='*60}")
            print(f"❌ Error: {str(e)}")
            print(f"🔄 Reiniciando en 5 segundos... (Intento #{reconnect_count})")
            print(f"{'='*60}\n")
            
            # Log del error completo para debugging
            import traceback
            print(f"📋 Traceback completo:")
            traceback.print_exc()
            print()
            
            await asyncio.sleep(5)
            continue


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() ya canceló todas las tareas y cerró el loop.
        # Silenciamos la excepción para evitar el traceback en consola.
        pass
    finally:
        # Se ejecuta siempre: con Ctrl+C, con error o con cierre normal.
        # El loop ya está destruido — generar_resumen_salida() es 100 % síncrona.
        print("\n\n🛑 DETENIENDO EL BOT... GENERANDO REPORTE DE SESION 🛑")
        generar_resumen_salida()
