"""
Meta-Estrategia Auto Hybrid - Selector Automático Inteligente

Esta es una "Meta-Estrategia" que analiza las condiciones del mercado y selecciona
automáticamente la estrategia más adecuada según la volatilidad detectada.

LÓGICA DE SELECCIÓN:
--------------------
1. Calcula BB Width (Ancho de Bollinger Bands) como medida de volatilidad
2. Compara BB Width actual con promedio histórico
3. Selecciona estrategia según condiciones:

   - BB Width BAJO (< promedio histórico):
     → Mercado en consolidación/Squeeze
     → Usa: Reversión o Breakout
     
   - BB Width ALTO (>= promedio histórico):
     → Mercado con tendencia fuerte
     → Usa: Trend Scalper

VENTAJAS:
---------
- Se adapta automáticamente a las condiciones del mercado
- Optimiza la estrategia según volatilidad
- Combina lo mejor de múltiples estrategias
- Reduce operaciones en condiciones desfavorables

ESTRATEGIAS DISPONIBLES:
------------------------
- strategies.reversion: Para mercados laterales/consolidados
- strategies.trend_scalper: Para mercados con tendencia fuerte
- strategies.breakout: Para detectar explosiones después de Squeeze
"""

import pandas as pd
import numpy as np
from ta.volatility import BollingerBands

# Importar estrategias subordinadas
import strategies.reversion as reversion_strategy
import strategies.trend_scalper as trend_scalper_strategy
import strategies.breakout as breakout_strategy


def analyze(df: pd.DataFrame) -> dict:
    """
    Analiza el DataFrame y selecciona automáticamente la mejor estrategia según volatilidad.
    
    Args:
        df: DataFrame con columnas ['open', 'high', 'low', 'close']
            Debe tener al menos 50 velas para análisis completo.
    
    Returns:
        dict: {
            'action': 'call' | 'put' | None,
            'message': str  # Mensaje descriptivo con estrategia seleccionada
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
    
    # Validar mínimo de datos (necesitamos al menos 50 para análisis completo)
    if len(df) < 50:
        return {
            'action': None,
            'message': f"Insuficientes datos para Auto Hybrid (requiere 50 velas, hay {len(df)})"
        }
    
    try:
        # === 1. CALCULAR BB WIDTH (Medida de Volatilidad) ===
        bb_indicator = BollingerBands(close=df['close'], window=20, window_dev=2.0)
        bb_upper = bb_indicator.bollinger_hband()
        bb_lower = bb_indicator.bollinger_lband()
        bb_middle = bb_indicator.bollinger_mavg()
        
        # Calcular BB Width = (Banda Superior - Banda Inferior) / Banda Media
        bb_width = (bb_upper - bb_lower) / bb_middle
        
        # Obtener valores válidos
        valid_bb_width = bb_width.dropna()
        if len(valid_bb_width) < 10:
            return {
                'action': None,
                'message': "BB Width insuficiente para análisis de volatilidad"
            }
        
        current_bb_width = valid_bb_width.iloc[-1]
        
        # Calcular promedio histórico (últimas 20 velas o todas si hay menos)
        lookback_period = min(20, len(valid_bb_width))
        historical_bb_width = valid_bb_width.iloc[-lookback_period:]
        avg_bb_width = historical_bb_width.mean()
        
        # Calcular desviación estándar para determinar umbrales
        std_bb_width = historical_bb_width.std()
        
        # === 2. CLASIFICAR VOLATILIDAD ===
        # Baja volatilidad: BB Width < promedio - 0.5 * desviación estándar
        # Alta volatilidad: BB Width >= promedio
        LOW_VOLATILITY_THRESHOLD = avg_bb_width - (0.5 * std_bb_width)
        is_low_volatility = current_bb_width < LOW_VOLATILITY_THRESHOLD
        is_high_volatility = current_bb_width >= avg_bb_width
        
        # === 3. SELECCIÓN DE ESTRATEGIA ===
        selected_strategy = None
        strategy_name = ""
        result = None
        
        if is_low_volatility:
            # Mercado en consolidación/Squeeze
            # Prioridad 1: Buscar Breakout (explosión después de Squeeze)
            # Prioridad 2: Si no hay breakout, usar Reversión
            
            try:
                result = breakout_strategy.analyze(df)
                if result.get('action') is not None:
                    selected_strategy = "Breakout"
                    strategy_name = "Momentum Breakout"
                else:
                    # No hay señal de breakout, intentar Reversión
                    result = reversion_strategy.analyze(df)
                    if result.get('action') is not None:
                        selected_strategy = "Reversion"
                        strategy_name = "Reversión"
                    else:
                        selected_strategy = "Breakout"
                        strategy_name = "Momentum Breakout (sin señal)"
            except Exception as e:
                # Si Breakout falla, usar Reversión como fallback
                try:
                    result = reversion_strategy.analyze(df)
                    selected_strategy = "Reversion"
                    strategy_name = "Reversión"
                except:
                    return {
                        'action': None,
                        'message': f"Error en estrategias de baja volatilidad: {str(e)}"
                    }
        
        elif is_high_volatility:
            # Mercado con tendencia fuerte
            # Usar Trend Scalper
            try:
                result = trend_scalper_strategy.analyze(df)
                selected_strategy = "TrendScalper"
                strategy_name = "Trend Scalper"
            except Exception as e:
                return {
                    'action': None,
                    'message': f"Error en Trend Scalper: {str(e)}"
                }
        
        else:
            # Volatilidad media
            # Intentar Trend Scalper primero, luego Breakout
            try:
                result = trend_scalper_strategy.analyze(df)
                if result.get('action') is not None:
                    selected_strategy = "TrendScalper"
                    strategy_name = "Trend Scalper"
                else:
                    result = breakout_strategy.analyze(df)
                    selected_strategy = "Breakout"
                    strategy_name = "Momentum Breakout"
            except Exception as e:
                return {
                    'action': None,
                    'message': f"Error en estrategias de volatilidad media: {str(e)}"
                }
        
        # === 4. FORMATEAR RESPUESTA ===
        if result is None:
            return {
                'action': None,
                'message': "No se pudo obtener resultado de ninguna estrategia"
            }
        
        action = result.get('action')
        original_message = result.get('message', 'Sin mensaje')
        
        # Agregar información sobre la estrategia seleccionada
        volatility_status = "BAJA" if is_low_volatility else ("ALTA" if is_high_volatility else "MEDIA")
        enhanced_message = (
            f"🤖 AUTO: {strategy_name} | "
            f"Volatilidad: {volatility_status} "
            f"(BB Width: {current_bb_width:.6f} vs promedio: {avg_bb_width:.6f}) | "
            f"{original_message}"
        )
        
        return {
            'action': action,
            'message': enhanced_message
        }
        
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
            'message': f"Error en Auto Hybrid: {str(e)} | Traceback: {traceback.format_exc()[:200]}"
        }

