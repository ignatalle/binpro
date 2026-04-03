"""
Estrategia Hyper Scalper - Modo Ametralladora (Alta Frecuencia)

Esta estrategia está basada en los principios de Larry Connors pero acelerada para
trading de altísima frecuencia. Diseñada para capturar movimientos rápidos en mercados
volátiles con señales muy frecuentes.

LÓGICA DE LA ESTRATEGIA:
------------------------
1. RSI Ultra Rápido (window=4):
   - RSI de 4 períodos para detectar condiciones extremas rápidamente
   - RSI < 30: Sobreventa (oportunidad de compra)
   - RSI > 70: Sobrecompra (oportunidad de venta)
   - Responde mucho más rápido que RSI estándar (14 períodos)

2. EMA Micro Tendencia (window=20):
   - EMA de 20 períodos para identificar dirección de tendencia micro
   - Precio > EMA20: Tendencia alcista micro
   - Precio < EMA20: Tendencia bajista micro

REGLAS DE OPERACIÓN (Larry Connors Acelerado):
-----------------------------------------------
- CALL (Compra): 
  * Precio > EMA20 (tendencia alcista micro)
  * Y RSI < 30 (sobreventa - comprar el dip en subida)
  * Lógica: Comprar pullbacks en tendencia alcista

- PUT (Venta):
  * Precio < EMA20 (tendencia bajista micro)
  * Y RSI > 70 (sobrecompra - vender el rebote en bajada)
  * Lógica: Vender rebotes en tendencia bajista

- WAIT: Si no se cumplen las condiciones anteriores

CARACTERÍSTICAS:
---------------
- Altísima frecuencia de señales
- Requiere solo 20 velas (muy rápido de activar)
- Ideal para mercados volátiles con movimientos rápidos
- Basado en mean reversion acelerada
- Captura movimientos de corto plazo muy rápidamente

ADVERTENCIA:
-----------
Esta estrategia genera señales MUY frecuentes. Úsala con precaución y gestión
de riesgo adecuada. No recomendada para principiantes.
"""

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator


def analyze(df: pd.DataFrame) -> dict:
    """
    Analiza el DataFrame de velas y retorna una señal de trading de alta frecuencia.
    
    Args:
        df: DataFrame con columnas ['open', 'high', 'low', 'close']
            Debe tener al menos 20 velas para calcular indicadores correctamente.
    
    Returns:
        dict: {
            'action': 'call' | 'put' | None,
            'message': str  # Mensaje descriptivo con formato "HYPER: [Razón]"
        }
    """
    # === VALIDACIÓN INICIAL ===
    if df is None or len(df) == 0:
        return {
            'action': None,
            'message': "HYPER: DataFrame vacío o None"
        }
    
    # Validar columnas necesarias
    required_columns = ['open', 'high', 'low', 'close']
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        return {
            'action': None,
            'message': f"HYPER: Faltan columnas requeridas: {missing_columns}"
        }
    
    # Validar mínimo de datos (20 velas)
    if len(df) < 20:
        return {
            'action': None,
            'message': f"HYPER: Insuficientes datos (requiere 20 velas, hay {len(df)})"
        }
    
    # Validar que no haya valores NaN en close
    if df['close'].isna().any():
        return {
            'action': None,
            'message': "HYPER: Datos inválidos (valores NaN en columna 'close')"
        }
    
    try:
        # === 1. CALCULAR RSI ULTRA RÁPIDO (window=4) ===
        rsi_indicator = RSIIndicator(close=df['close'], window=4)
        rsi = rsi_indicator.rsi()
        
        # Validar que RSI tenga valores válidos
        if rsi.isna().all():
            return {
                'action': None,
                'message': "HYPER: No se pudo calcular RSI (todos los valores son NaN)"
            }
        
        # Obtener último valor válido
        valid_rsi = rsi.dropna()
        if len(valid_rsi) == 0:
            return {
                'action': None,
                'message': "HYPER: RSI no tiene valores válidos"
            }
        
        current_rsi = valid_rsi.iloc[-1]
        
        # Validar que RSI sea un número válido
        if pd.isna(current_rsi) or not isinstance(current_rsi, (int, float)):
            return {
                'action': None,
                'message': f"HYPER: RSI inválido: {current_rsi}"
            }
        
        # === 2. CALCULAR EMA MICRO TENDENCIA (window=20) ===
        ema_indicator = EMAIndicator(close=df['close'], window=20)
        ema_20 = ema_indicator.ema_indicator()
        
        # Validar que EMA tenga valores válidos
        if ema_20.isna().all():
            return {
                'action': None,
                'message': "HYPER: No se pudo calcular EMA 20 (todos los valores son NaN)"
            }
        
        # Obtener último valor válido
        valid_ema = ema_20.dropna()
        if len(valid_ema) == 0:
            return {
                'action': None,
                'message': "HYPER: EMA 20 no tiene valores válidos"
            }
        
        current_ema = valid_ema.iloc[-1]
        
        # Validar que EMA sea un número válido
        if pd.isna(current_ema) or not isinstance(current_ema, (int, float)):
            return {
                'action': None,
                'message': f"HYPER: EMA 20 inválido: {current_ema}"
            }
        
        # === 3. PRECIO ACTUAL ===
        current_price = float(df['close'].iloc[-1])
        
        # Validar precio
        if pd.isna(current_price) or current_price <= 0:
            return {
                'action': None,
                'message': f"HYPER: Precio actual inválido: {current_price}"
            }
        
        # === 4. LÓGICA DE DECISIÓN (Larry Connors Acelerado) ===
        
        # CALL: Precio > EMA20 Y RSI < 30 (Comprar el dip en subida)
        if current_price > current_ema and current_rsi < 30:
            reason = (
                f"Comprar dip en subida | "
                f"Precio {current_price:.5f} > EMA20 {current_ema:.5f} | "
                f"RSI {current_rsi:.2f} < 30 (sobreventa)"
            )
            return {
                'action': 'call',
                'message': f"HYPER: {reason}"
            }
        
        # PUT: Precio < EMA20 Y RSI > 70 (Vender el rebote en bajada)
        elif current_price < current_ema and current_rsi > 70:
            reason = (
                f"Vender rebote en bajada | "
                f"Precio {current_price:.5f} < EMA20 {current_ema:.5f} | "
                f"RSI {current_rsi:.2f} > 70 (sobrecompra)"
            )
            return {
                'action': 'put',
                'message': f"HYPER: {reason}"
            }
        
        # WAIT: No se cumplen las condiciones
        else:
            reasons = []
            
            if current_price > current_ema:
                reasons.append(f"Tendencia alcista (Precio {current_price:.5f} > EMA20 {current_ema:.5f})")
            else:
                reasons.append(f"Tendencia bajista (Precio {current_price:.5f} < EMA20 {current_ema:.5f})")
            
            if current_rsi < 30:
                reasons.append(f"RSI en sobreventa ({current_rsi:.2f} < 30) pero sin tendencia alcista")
            elif current_rsi > 70:
                reasons.append(f"RSI en sobrecompra ({current_rsi:.2f} > 70) pero sin tendencia bajista")
            else:
                reasons.append(f"RSI en zona neutral ({current_rsi:.2f})")
            
            return {
                'action': None,
                'message': f"HYPER: WAIT - {' | '.join(reasons)}"
            }
            
    except KeyError as e:
        return {
            'action': None,
            'message': f"HYPER: Error de clave en DataFrame: {str(e)}"
        }
    except ValueError as e:
        return {
            'action': None,
            'message': f"HYPER: Error de valor en cálculo: {str(e)}"
        }
    except Exception as e:
        import traceback
        return {
            'action': None,
            'message': f"HYPER: Error en Hyper Scalper: {str(e)} | Traceback: {traceback.format_exc()[:200]}"
        }

