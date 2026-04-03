"""
Estrategia Reversión - Para Mercados Laterales/Forex

Esta estrategia está diseñada para activos estables como pares de divisas (Forex)
y mercados que tienden a moverse en rangos laterales. Se basa en el principio de
que los precios tienden a revertir hacia la media después de alcanzar extremos.

LÓGICA DE LA ESTRATEGIA:
------------------------
1. RSI 14 (Relative Strength Index):
   - Mide la fuerza del movimiento de precios
   - RSI > 70 → Sobrecompra (posible reversión bajista)
   - RSI < 30 → Sobreventa (posible reversión alcista)
   - Ideal para detectar condiciones extremas en mercados estables

2. Bollinger Bands (Bandas de Bollinger - 20 períodos, 2.0 desviaciones):
   - Identifica niveles de soporte y resistencia dinámicos
   - Banda Superior → Resistencia (zona de sobrecompra)
   - Banda Inferior → Soporte (zona de sobreventa)
   - En mercados laterales, el precio rebota entre las bandas

POR QUÉ FUNCIONA EN FOREX/MERCADOS ESTABLES:
--------------------------------------------
- Los pares de divisas tienden a moverse en rangos definidos
- El RSI identifica condiciones extremas antes de que ocurra la reversión
- Las Bollinger Bands proporcionan niveles objetivos de entrada y salida
- La combinación reduce operaciones en falsos breakouts
- Funciona mejor en mercados con volatilidad moderada y predecible

REGLAS DE OPERACIÓN:
--------------------
- PUT (Venta): Precio >= Banda Superior AND RSI > 70
  → Señal de sobrecompra extrema, esperamos reversión bajista
  
- CALL (Compra): Precio <= Banda Inferior AND RSI < 30
  → Señal de sobreventa extrema, esperamos reversión alcista
  
- WAIT: Si no se cumplen las condiciones anteriores
  → Evitamos operar en zonas intermedias donde la señal no es clara

VENTAJAS:
---------
- Alta precisión en mercados laterales
- Señales claras y objetivas
- Reduce operaciones en zonas de incertidumbre
- Ideal para activos con volatilidad moderada
"""

import pandas as pd
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands


def analyze(df: pd.DataFrame) -> dict:
    """
    Analiza el DataFrame de velas y retorna una señal de trading basada en reversión.
    
    Args:
        df: DataFrame con columnas ['open', 'high', 'low', 'close']
            Debe tener al menos 20 velas para calcular Bollinger Bands correctamente.
    
    Returns:
        dict: {
            'action': 'call' | 'put' | None,
            'message': str  # Mensaje descriptivo de la decisión
        }
    """
    if len(df) < 20:
        return {
            'action': None,
            'message': f"Insuficientes datos para Reversión (requiere 20 velas, hay {len(df)})"
        }
    
    try:
        # === PARÁMETROS DE LA ESTRATEGIA ===
        RSI_PERIOD = 14
        RSI_OVERBOUGHT = 70
        RSI_OVERSOLD = 30
        BB_WINDOW = 20
        BB_STD = 2.0
        
        # === 1. CALCULAR RSI (14 períodos) ===
        rsi_indicator = RSIIndicator(close=df['close'], window=RSI_PERIOD)
        rsi = rsi_indicator.rsi()
        current_rsi = rsi.iloc[-1]
        
        # === 2. CALCULAR BOLLINGER BANDS (20, 2.0) ===
        bb_indicator = BollingerBands(close=df['close'], window=BB_WINDOW, window_dev=BB_STD)
        bb_upper = bb_indicator.bollinger_hband().iloc[-1]
        bb_lower = bb_indicator.bollinger_lband().iloc[-1]
        bb_middle = bb_indicator.bollinger_mavg().iloc[-1]
        
        # === 3. PRECIO ACTUAL ===
        current_price = df['close'].iloc[-1]
        
        # === 4. LÓGICA DE DECISIÓN ===
        
        # === REGLA PUT (Venta): Sobrecompra Extrema ===
        # Condición: Precio en o por encima de banda superior Y RSI sobrecomprado
        if current_price >= bb_upper and current_rsi > RSI_OVERBOUGHT:
            message = (
                f"PUT: Sobrecompra extrema detectada | "
                f"Precio {current_price:.5f} >= BB Superior {bb_upper:.5f} | "
                f"RSI {current_rsi:.2f} > {RSI_OVERBOUGHT} (sobrecomprado) | "
                f"Esperando reversión bajista hacia la media"
            )
            return {'action': 'put', 'message': message}
        
        # === REGLA CALL (Compra): Sobreventa Extrema ===
        # Condición: Precio en o por debajo de banda inferior Y RSI sobrevendido
        elif current_price <= bb_lower and current_rsi < RSI_OVERSOLD:
            message = (
                f"CALL: Sobreventa extrema detectada | "
                f"Precio {current_price:.5f} <= BB Inferior {bb_lower:.5f} | "
                f"RSI {current_rsi:.2f} < {RSI_OVERSOLD} (sobrevendido) | "
                f"Esperando reversión alcista hacia la media"
            )
            return {'action': 'call', 'message': message}
        
        # === WAIT: No se cumplen las condiciones ===
        else:
            reasons = []
            
            # Analizar por qué no hay señal
            if current_price < bb_upper:
                reasons.append(f"Precio {current_price:.5f} < BB Superior {bb_upper:.5f}")
            else:
                reasons.append(f"Precio {current_price:.5f} >= BB Superior {bb_upper:.5f} pero RSI {current_rsi:.2f} <= {RSI_OVERBOUGHT}")
            
            if current_price > bb_lower:
                reasons.append(f"Precio {current_price:.5f} > BB Inferior {bb_lower:.5f}")
            else:
                reasons.append(f"Precio {current_price:.5f} <= BB Inferior {bb_lower:.5f} pero RSI {current_rsi:.2f} >= {RSI_OVERSOLD}")
            
            message = f"WAIT: {' | '.join(reasons)}"
            return {'action': None, 'message': message}
            
    except Exception as e:
        return {
            'action': None,
            'message': f"Error en Reversión: {str(e)}"
        }

