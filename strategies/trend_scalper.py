"""
Estrategia Trend Scalper - Para Mercados Volátiles/Crypto

Esta estrategia está diseñada específicamente para activos volátiles como criptomonedas
y índices (CRY/IDX). Utiliza una combinación de indicadores técnicos que aprovechan
los movimientos rápidos y tendenciales característicos de estos mercados.

LÓGICA DE LA ESTRATEGIA:
------------------------
1. EMA 50 (Media Móvil Exponencial de 50 períodos):
   - Identifica la dirección de la tendencia principal
   - Si precio > EMA 50 → Tendencia alcista
   - Si precio < EMA 50 → Tendencia bajista
   - SOLO operamos a favor de la tendencia (no contra-tendencia)

2. StochRSI (Stochastic RSI):
   - Proporciona señales de entrada precisas (gatillo)
   - Detecta condiciones de sobrecompra/sobreventa en el RSI
   - Cruces por encima/por debajo de niveles críticos generan señales

3. Bollinger Bands (Bandas de Bollinger):
   - Identifica extremos de precio (sobrecompra/sobreventa)
   - Las bandas se expanden en volatilidad alta (ideal para crypto)
   - Precio cerca de banda superior → Posible reversión bajista
   - Precio cerca de banda inferior → Posible reversión alcista

POR QUÉ FUNCIONA EN CRYPTO/IDX:
--------------------------------
- Los activos volátiles tienen movimientos rápidos y tendenciales
- La EMA 50 filtra el ruido y captura la dirección principal
- StochRSI ofrece entradas precisas sin esperar extremos absolutos
- Bollinger Bands se adaptan a la volatilidad cambiante de estos mercados
- La combinación reduce falsas señales en mercados laterales

REGLAS DE OPERACIÓN:
--------------------
- CALL (Compra): Precio > EMA 50 AND StochRSI cruza por encima de nivel de sobreventa AND precio cerca de banda inferior
- PUT (Venta): Precio < EMA 50 AND StochRSI cruza por debajo de nivel de sobrecompra AND precio cerca de banda superior
- WAIT: Si no se cumplen las condiciones anteriores
"""

import pandas as pd
from ta.trend import EMAIndicator
from ta.momentum import StochRSIIndicator
from ta.volatility import BollingerBands


def analyze(df: pd.DataFrame) -> dict:
    """
    Analiza el DataFrame de velas y retorna una señal de trading.
    
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
    if len(df) < 50:
        return {
            'action': None,
            'message': f"Insuficientes datos para Trend Scalper (requiere 50 velas, hay {len(df)})"
        }
    
    # Validar que no haya valores NaN en close
    if df['close'].isna().any():
        return {
            'action': None,
            'message': "Datos inválidos: valores NaN encontrados en columna 'close'"
        }
    
    try:
        # === 1. CALCULAR EMA 50 (Tendencia Principal) ===
        ema_indicator = EMAIndicator(close=df['close'], window=50)
        ema_50 = ema_indicator.ema_indicator()
        
        # Validar que EMA tenga valores válidos
        if ema_50.isna().all():
            return {
                'action': None,
                'message': "No se pudo calcular EMA 50 (todos los valores son NaN)"
            }
        
        # Obtener últimos valores válidos (saltando NaN)
        valid_ema = ema_50.dropna()
        if len(valid_ema) == 0:
            return {
                'action': None,
                'message': "EMA 50 no tiene valores válidos"
            }
        
        current_ema = valid_ema.iloc[-1]
        previous_ema = valid_ema.iloc[-2] if len(valid_ema) > 1 else current_ema
        
        # Validar que EMA sea un número válido
        if pd.isna(current_ema) or not isinstance(current_ema, (int, float)):
            return {
                'action': None,
                'message': f"EMA 50 inválido: {current_ema}"
            }
        
        # === 2. CALCULAR StochRSI (Gatillo de Entrada) ===
        stoch_rsi_indicator = StochRSIIndicator(close=df['close'], window=14, smooth1=3, smooth2=3)
        stoch_rsi = stoch_rsi_indicator.stochrsi()
        
        # Validar que StochRSI tenga valores válidos
        if stoch_rsi.isna().all():
            return {
                'action': None,
                'message': "No se pudo calcular StochRSI (todos los valores son NaN)"
            }
        
        # Obtener últimos valores válidos (saltando NaN)
        valid_stoch_rsi = stoch_rsi.dropna()
        if len(valid_stoch_rsi) < 2:
            return {
                'action': None,
                'message': f"StochRSI insuficiente (solo {len(valid_stoch_rsi)} valores válidos, se necesitan 2)"
            }
        
        current_stoch_rsi = valid_stoch_rsi.iloc[-1]
        previous_stoch_rsi = valid_stoch_rsi.iloc[-2] if len(valid_stoch_rsi) > 1 else current_stoch_rsi
        
        # Validar que StochRSI sea un número válido
        if pd.isna(current_stoch_rsi) or not isinstance(current_stoch_rsi, (int, float)):
            return {
                'action': None,
                'message': f"StochRSI actual inválido: {current_stoch_rsi}"
            }
        if pd.isna(previous_stoch_rsi) or not isinstance(previous_stoch_rsi, (int, float)):
            previous_stoch_rsi = current_stoch_rsi  # Fallback si el anterior es inválido
        
        # Niveles de StochRSI (0-1, donde 0.2 = sobreventa, 0.8 = sobrecompra)
        STOCHRSI_OVERSOLD = 0.2
        STOCHRSI_OVERBOUGHT = 0.8
        
        # === 3. CALCULAR BOLLINGER BANDS (Extremos) ===
        bb_indicator = BollingerBands(close=df['close'], window=20, window_dev=2.0)
        bb_upper = bb_indicator.bollinger_hband().iloc[-1]
        bb_lower = bb_indicator.bollinger_lband().iloc[-1]
        bb_middle = bb_indicator.bollinger_mavg().iloc[-1]
        
        # Validar que las bandas sean válidas
        if pd.isna(bb_upper) or pd.isna(bb_lower) or pd.isna(bb_middle):
            return {
                'action': None,
                'message': "No se pudieron calcular Bollinger Bands (valores NaN)"
            }
        
        # === 4. PRECIO ACTUAL ===
        current_price = float(df['close'].iloc[-1])
        
        # Validar precio
        if pd.isna(current_price) or current_price <= 0:
            return {
                'action': None,
                'message': f"Precio actual inválido: {current_price}"
            }
        
        # === 5. LÓGICA DE DECISIÓN ===
        
        # Determinar si estamos en tendencia alcista o bajista
        is_uptrend = current_price > current_ema
        
        # Calcular distancia a las bandas (normalizada)
        bb_range = bb_upper - bb_lower
        if bb_range <= 0:
            return {
                'action': None,
                'message': f"Rango de Bollinger Bands inválido: {bb_range}"
            }
        
        distance_to_upper = (bb_upper - current_price) / bb_range
        distance_to_lower = (current_price - bb_lower) / bb_range
        
        # Consideramos "cerca" de una banda si está a menos del 10% del rango
        NEAR_BAND_THRESHOLD = 0.1
        near_upper_band = distance_to_upper < NEAR_BAND_THRESHOLD
        near_lower_band = distance_to_lower < NEAR_BAND_THRESHOLD
        
        # Detectar cruces de StochRSI (mejorado: más flexible)
        stoch_rsi_crossed_above_oversold = (
            previous_stoch_rsi <= STOCHRSI_OVERSOLD and 
            current_stoch_rsi > STOCHRSI_OVERSOLD
        )
        stoch_rsi_crossed_below_overbought = (
            previous_stoch_rsi >= STOCHRSI_OVERBOUGHT and 
            current_stoch_rsi < STOCHRSI_OVERBOUGHT
        )
        
        # También considerar si StochRSI está en zona de sobreventa/sobrecompra sin cruce
        in_oversold_zone = current_stoch_rsi <= STOCHRSI_OVERSOLD
        in_overbought_zone = current_stoch_rsi >= STOCHRSI_OVERBOUGHT
        
        # === REGLA CALL (Compra): Tendencia Alcista + Gatillo + Extremo Inferior ===
        if is_uptrend and stoch_rsi_crossed_above_oversold and near_lower_band:
            message = (
                f"CALL: Tendencia alcista (Precio {current_price:.5f} > EMA50 {current_ema:.5f}) | "
                f"StochRSI cruza sobreventa ({previous_stoch_rsi:.3f} → {current_stoch_rsi:.3f}) | "
                f"Precio cerca banda inferior ({bb_lower:.5f})"
            )
            return {'action': 'call', 'message': message}
        
        # Variante CALL: Tendencia alcista + sobreventa + banda inferior (sin cruce)
        elif is_uptrend and in_oversold_zone and near_lower_band:
            message = (
                f"CALL: Tendencia alcista (Precio {current_price:.5f} > EMA50 {current_ema:.5f}) | "
                f"StochRSI en sobreventa ({current_stoch_rsi:.3f}) | "
                f"Precio cerca banda inferior ({bb_lower:.5f})"
            )
            return {'action': 'call', 'message': message}
        
        # === REGLA PUT (Venta): Tendencia Bajista + Gatillo + Extremo Superior ===
        elif not is_uptrend and stoch_rsi_crossed_below_overbought and near_upper_band:
            message = (
                f"PUT: Tendencia bajista (Precio {current_price:.5f} < EMA50 {current_ema:.5f}) | "
                f"StochRSI cruza sobrecompra ({previous_stoch_rsi:.3f} → {current_stoch_rsi:.3f}) | "
                f"Precio cerca banda superior ({bb_upper:.5f})"
            )
            return {'action': 'put', 'message': message}
        
        # Variante PUT: Tendencia bajista + sobrecompra + banda superior (sin cruce)
        elif not is_uptrend and in_overbought_zone and near_upper_band:
            message = (
                f"PUT: Tendencia bajista (Precio {current_price:.5f} < EMA50 {current_ema:.5f}) | "
                f"StochRSI en sobrecompra ({current_stoch_rsi:.3f}) | "
                f"Precio cerca banda superior ({bb_upper:.5f})"
            )
            return {'action': 'put', 'message': message}
        
        # === WAIT: No se cumplen las condiciones ===
        else:
            reasons = []
            if not is_uptrend:
                reasons.append(f"Tendencia bajista (Precio {current_price:.5f} < EMA50 {current_ema:.5f})")
            else:
                reasons.append(f"Tendencia alcista (Precio {current_price:.5f} > EMA50 {current_ema:.5f})")
            
            if not stoch_rsi_crossed_above_oversold and not stoch_rsi_crossed_below_overbought:
                if not in_oversold_zone and not in_overbought_zone:
                    reasons.append(f"StochRSI en zona neutral ({current_stoch_rsi:.3f})")
                elif in_oversold_zone:
                    reasons.append(f"StochRSI en sobreventa pero sin señal de cruce ({current_stoch_rsi:.3f})")
                elif in_overbought_zone:
                    reasons.append(f"StochRSI en sobrecompra pero sin señal de cruce ({current_stoch_rsi:.3f})")
            
            if not near_lower_band and not near_upper_band:
                reasons.append(f"Precio en zona media de BB (distancia superior: {distance_to_upper:.2%}, inferior: {distance_to_lower:.2%})")
            
            message = f"WAIT: {' | '.join(reasons)}"
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
            'message': f"Error en Trend Scalper: {str(e)} | Traceback: {traceback.format_exc()[:200]}"
        }

