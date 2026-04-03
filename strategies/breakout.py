"""
Estrategia Sniper Breakout Triple Confluence "ONE SHOT" - Optimizada para Z-CRY/IDX

Esta estrategia está diseñada para maximizar el Win Rate en activos de baja volatilidad
como Z-CRY/IDX, utilizando una triple confluencia de señales para filtrar operaciones
de alta probabilidad.

🎯 LÓGICA "ONE SHOT" (ANTI-OVERTRADING):
-----------------------------------------
La estrategia opera SOLO en el momento exacto de la ruptura, NO en velas posteriores
mientras el precio permanece fuera de las bandas. Esto previene el overtrading y
mejora el timing de entrada.

- ✅ Opera: Cuando el precio CRUZA la banda (vela actual rompe, vela anterior dentro)
- ❌ NO Opera: Cuando el precio YA ESTÁ fuera de la banda desde velas anteriores

LÓGICA DE LA ESTRATEGIA:
------------------------
1. Bollinger Bands (20 períodos, 2.0 desviaciones):
   - Detecta rupturas exactas de bandas (breakouts "one shot")
   - Ancho de bandas para detectar compresión (micro-squeeze)

2. EMA 50 (Tendencia):
   - Filtro direccional: solo operamos a favor de la tendencia
   - CALL: precio debe estar por encima de EMA 50
   - PUT: precio debe estar por debajo de EMA 50

3. RSI 14 (Momentum/Agotamiento):
   - Filtro de agotamiento: evitamos operar en zonas extremas
   - CALL: RSI < 85 (evita sobrecompra extrema)
   - PUT: RSI > 15 (evita sobreventa extrema)

TRIPLE CONFLUENCIA:
------------------
Para generar una señal válida, deben cumplirse las 3 condiciones simultáneamente:
- Breakout exacto de banda (momento preciso del cruce)
- Tendencia favorable (precio vs EMA 50)
- Momentum no extremo (RSI en rango válido)

POR QUÉ FUNCIONA:
-----------------
- La triple confluencia reduce falsos breakouts
- El filtro de tendencia aumenta probabilidad de éxito
- El filtro RSI evita entrar en zonas de agotamiento
- Lógica "One Shot" previene overtrading y mejora el timing
- Optimizado para activos de baja volatilidad con precisión micro-decimal

REGLAS DE OPERACIÓN:
--------------------
- CALL (Compra): 
  * Precio ROMPE banda superior (cruce exacto, no después)
  * Precio > EMA 50 (tendencia alcista)
  * RSI < 85 (no sobrecomprado extremo)
  
- PUT (Venta):
  * Precio ROMPE banda inferior (cruce exacto, no después)
  * Precio < EMA 50 (tendencia bajista)
  * RSI > 15 (no sobrevendido extremo)
  
- WAIT: Si no se cumplen las 3 condiciones simultáneamente EN EL MOMENTO DEL CRUCE
"""

import pandas as pd
import numpy as np
from ta.volatility import BollingerBands
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator

# === CONFIGURACIÓN ===
MIN_CANDLES = 50  # Necesario para EMA 50
BB_WINDOW = 20
BB_STD = 2.0
EMA_TREND = 50
RSI_WINDOW = 14
RSI_CEILING = 85  # Límite superior para CALL (ajustado para Crypto)
RSI_FLOOR = 15    # Límite inferior para PUT (ajustado para Crypto)
MICRO_SQUEEZE_LIMIT = 1.0e-7  # Para detectar volatilidad microscópica en Z-CRY/IDX


def analyze(df: pd.DataFrame) -> dict:
    """
    Analiza el DataFrame de velas y retorna una señal de trading basada en 
    Sniper Breakout Triple Confluence.
    
    Args:
        df: DataFrame con columnas ['open', 'high', 'low', 'close']
            Debe tener al menos 50 velas para calcular EMA 50 correctamente.
    
    Returns:
        dict: {
            'action': 'call' | 'put' | None,
            'message': str  # Mensaje descriptivo de la decisión
        }
    """
    # === VALIDACIÓN INICIAL ===
    if df is None or len(df) == 0:
        return {
            'action': None,
            'message': "DataFrame vacío o None"
        }
    
    # Validar columnas necesarias
    required_columns = ['open', 'high', 'low', 'close']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        return {
            'action': None,
            'message': f"Faltan columnas requeridas: {missing_columns}"
        }
    
    # Validar mínimo de datos
    if len(df) < MIN_CANDLES:
        return {
            'action': None,
            'message': f"⏳ CALIBRANDO... (requiere {MIN_CANDLES} velas, hay {len(df)})"
        }
    
    # Validar que no haya valores NaN en close
    if df['close'].isna().any():
        return {
            'action': None,
            'message': "Datos inválidos: valores NaN encontrados en columna 'close'"
        }
    
    try:
        # === 1. CALCULAR BOLLINGER BANDS ===
        bb_indicator = BollingerBands(close=df['close'], window=BB_WINDOW, window_dev=BB_STD)
        bb_upper = bb_indicator.bollinger_hband()
        bb_lower = bb_indicator.bollinger_lband()
        bb_middle = bb_indicator.bollinger_mavg()
        
        # Validar que las bandas sean válidas
        if bb_upper.isna().all() or bb_lower.isna().all():
            return {
                'action': None,
                'message': "No se pudieron calcular Bollinger Bands (valores NaN)"
            }
        
        # Obtener últimos valores válidos y convertir a float para precisión
        valid_bb_upper = bb_upper.dropna()
        valid_bb_lower = bb_lower.dropna()
        
        if len(valid_bb_upper) < 2 or len(valid_bb_lower) < 2:
            return {
                'action': None,
                'message': "Bollinger Bands insuficientes para análisis"
            }
        
        # Convertir a float para mantener precisión con valores micro-decimales
        current_bb_upper = float(valid_bb_upper.iloc[-1])
        current_bb_lower = float(valid_bb_lower.iloc[-1])
        previous_bb_upper = float(valid_bb_upper.iloc[-2]) if len(valid_bb_upper) > 1 else current_bb_upper
        previous_bb_lower = float(valid_bb_lower.iloc[-2]) if len(valid_bb_lower) > 1 else current_bb_lower
        
        # Validar valores
        if pd.isna(current_bb_upper) or pd.isna(current_bb_lower):
            return {
                'action': None,
                'message': "Bollinger Bands con valores inválidos"
            }
        
        # Calcular BB Width para logging (usar notación científica)
        bb_width = (current_bb_upper - current_bb_lower) / float(bb_middle.iloc[-1])
        bb_width_str = f"{bb_width:.2e}"
        
        # === 2. CALCULAR EMA 50 (Filtro de Tendencia) ===
        ema_indicator = EMAIndicator(close=df['close'], window=EMA_TREND)
        ema_50 = ema_indicator.ema_indicator()
        
        # Validar EMA
        if ema_50.isna().all():
            return {
                'action': None,
                'message': "No se pudo calcular EMA 50 (valores NaN)"
            }
        
        valid_ema = ema_50.dropna()
        if len(valid_ema) == 0:
            return {
                'action': None,
                'message': "EMA 50 insuficiente para análisis"
            }
        
        # Convertir a float para precisión
        current_ema_50 = float(valid_ema.iloc[-1])
        
        if pd.isna(current_ema_50):
            return {
                'action': None,
                'message': "EMA 50 con valor inválido"
            }
        
        # === 3. CALCULAR RSI 14 (Filtro de Momentum/Agotamiento) ===
        rsi_indicator = RSIIndicator(close=df['close'], window=RSI_WINDOW)
        rsi = rsi_indicator.rsi()
        
        # Validar RSI
        if rsi.isna().all():
            return {
                'action': None,
                'message': "No se pudo calcular RSI (valores NaN)"
            }
        
        valid_rsi = rsi.dropna()
        if len(valid_rsi) == 0:
            return {
                'action': None,
                'message': "RSI insuficiente para análisis"
            }
        
        # Convertir a float
        current_rsi = float(valid_rsi.iloc[-1])
        
        if pd.isna(current_rsi):
            return {
                'action': None,
                'message': "RSI con valor inválido"
            }
        
        # === 4. PRECIO ACTUAL (convertir a float para precisión) ===
        current_price = float(df['close'].iloc[-1])
        previous_price = float(df['close'].iloc[-2]) if len(df) > 1 else current_price
        
        # Validar precio
        if pd.isna(current_price) or current_price <= 0:
            return {
                'action': None,
                'message': f"Precio actual inválido: {current_price}"
            }
        
        # === 5. DETECTAR RUPTURAS DE BANDAS ===
        # Ruptura hacia arriba: precio actual > banda superior Y precio anterior <= banda superior
        breakout_up = current_price > current_bb_upper and previous_price <= previous_bb_upper
        
        # Ruptura hacia abajo: precio actual < banda inferior Y precio anterior >= banda inferior
        breakout_down = current_price < current_bb_lower and previous_price >= previous_bb_lower
        
        # También considerar si el precio está claramente fuera de las bandas (breakout ya establecido)
        price_above_upper = current_price > current_bb_upper
        price_below_lower = current_price < current_bb_lower
        
        # === 6. LÓGICA DE DECISIÓN - TRIPLE CONFLUENCIA "ONE SHOT" ===
        # 🎯 ONE SHOT: Solo opera en el MOMENTO EXACTO de la ruptura, no después
        
        # === CALL: Triple Confluencia Alcista ===
        # Condición 1: Breakout hacia arriba (SOLO ruptura exacta, NO si ya está fuera)
        breakout_condition_call = breakout_up  # ✅ Eliminado "or price_above_upper"
        
        # Condición 2: Tendencia alcista (precio por encima de EMA 50)
        trend_condition_call = current_price > current_ema_50
        
        # Condición 3: RSI no extremo (menor a 85 para evitar sobrecompra)
        momentum_condition_call = current_rsi < RSI_CEILING
        
        # CALL válido solo si se cumplen las 3 condiciones EN EL MOMENTO DE LA RUPTURA
        if breakout_condition_call and trend_condition_call and momentum_condition_call:
            message = (
                f"CALL: Sniper Breakout ONE SHOT | "
                f"Ruptura exacta alcista ({current_price:.5f} > {current_bb_upper:.5f}) | "
                f"Tendencia alcista (Precio {current_price:.5f} > EMA50 {current_ema_50:.5f}) | "
                f"RSI favorable ({current_rsi:.2f} < {RSI_CEILING}) | "
                f"BB Width: {bb_width_str}"
            )
            return {'action': 'call', 'message': message}
        
        # === PUT: Triple Confluencia Bajista ===
        # Condición 1: Breakout hacia abajo (SOLO ruptura exacta, NO si ya está fuera)
        breakout_condition_put = breakout_down  # ✅ Eliminado "or price_below_lower"
        
        # Condición 2: Tendencia bajista (precio por debajo de EMA 50)
        trend_condition_put = current_price < current_ema_50
        
        # Condición 3: RSI no extremo (mayor a 15 para evitar sobreventa)
        momentum_condition_put = current_rsi > RSI_FLOOR
        
        # PUT válido solo si se cumplen las 3 condiciones EN EL MOMENTO DE LA RUPTURA
        if breakout_condition_put and trend_condition_put and momentum_condition_put:
            message = (
                f"PUT: Sniper Breakout ONE SHOT | "
                f"Ruptura exacta bajista ({current_price:.5f} < {current_bb_lower:.5f}) | "
                f"Tendencia bajista (Precio {current_price:.5f} < EMA50 {current_ema_50:.5f}) | "
                f"RSI favorable ({current_rsi:.2f} > {RSI_FLOOR}) | "
                f"BB Width: {bb_width_str}"
            )
            return {'action': 'put', 'message': message}
        
        # === WAIT: Explicar por qué no hay operación ===
        reasons = []
        
        # Analizar qué condición falta para CALL
        if breakout_condition_call and trend_condition_call and not momentum_condition_call:
            reasons.append(f"Rechazado CALL: RSI extremo ({current_rsi:.2f} >= {RSI_CEILING})")
        elif breakout_condition_call and not trend_condition_call and momentum_condition_call:
            reasons.append(f"Rechazado CALL: Contra-tendencia (Precio {current_price:.5f} <= EMA50 {current_ema_50:.5f})")
        elif not breakout_condition_call and trend_condition_call and momentum_condition_call:
            reasons.append(f"Rechazado CALL: Sin ruptura exacta (esperando cruce de banda)")
        
        # Analizar qué condición falta para PUT
        if breakout_condition_put and trend_condition_put and not momentum_condition_put:
            reasons.append(f"Rechazado PUT: RSI extremo ({current_rsi:.2f} <= {RSI_FLOOR})")
        elif breakout_condition_put and not trend_condition_put and momentum_condition_put:
            reasons.append(f"Rechazado PUT: Contra-tendencia (Precio {current_price:.5f} >= EMA50 {current_ema_50:.5f})")
        elif not breakout_condition_put and trend_condition_put and momentum_condition_put:
            reasons.append(f"Rechazado PUT: Sin ruptura exacta (esperando cruce de banda)")
        
        # Explicar posición del precio con contexto "One Shot"
        if not breakout_condition_call and not breakout_condition_put:
            if price_above_upper:
                reasons.append(f"🔒 ONE SHOT: Precio ya fuera de banda superior (sin cruce nuevo)")
            elif price_below_lower:
                reasons.append(f"🔒 ONE SHOT: Precio ya fuera de banda inferior (sin cruce nuevo)")
            else:
                reasons.append(f"Precio dentro de bandas (Superior: {current_bb_upper:.5f}, Inferior: {current_bb_lower:.5f})")
        
        # Si no hay razones específicas, dar resumen general
        if not reasons:
            status_summary = []
            status_summary.append(f"Ruptura: {'↑' if breakout_condition_call else '↓' if breakout_condition_put else 'Ninguna'}")
            status_summary.append(f"Tendencia: {'Alcista' if trend_condition_call else 'Bajista' if trend_condition_put else 'Neutral'}")
            status_summary.append(f"RSI: {current_rsi:.2f}")
            reasons.append(f"Esperando ruptura exacta con confluencia: {', '.join(status_summary)}")
        
        message = f"WAIT: {' | '.join(reasons)} | BB Width: {bb_width_str}"
        return {'action': None, 'message': message}
            
    except KeyError as e:
        return {
            'action': None,
            'message': f"Error de clave en DataFrame: {str(e)}"
        }
    except ValueError as e:
        return {
            'action': None,
            'message': f"Error de valor en cálculo: {str(e)}"
        }
    except Exception as e:
        import traceback
        return {
            'action': None,
            'message': f"Error en Sniper Breakout Triple Confluence: {str(e)} | Traceback: {traceback.format_exc()[:200]}"
        }
