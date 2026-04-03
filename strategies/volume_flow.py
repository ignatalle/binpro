# strategies/volume_flow.py
import time
import math
from datetime import datetime

# --- CONFIGURACION DE SENSIBILIDAD ---
LOOKBACK_SECONDS     = 15         # Ventana de tiempo para acumular volumen
WHALE_THRESHOLD      = 5_000_000  # Apuesta unica considerada Ballena
MIN_VOLUME_REQUIRED  = 50_000     # Volumen minimo total para filtro de datos

# --- FILTROS V5 (Informe Estrategia V5 — Reglas P0/P1) ---
HORAS_PERMITIDAS = {2, 3, 4, 10, 15, 16, 19, 20, 21}  # UTC
WHALE_AMT_MIN    = 28_000_000
WHALE_AMT_MAX    = 999_999_999
STD_MIN          = 8e-8    # Volatilidad muerta por debajo de este valor
STD_MAX_PUT      = 1.5e-7  # Volatilidad excesiva para operar PUT


def _rsi_defined(rsi):
    if rsi is None:
        return False
    try:
        r = float(rsi)
    except (TypeError, ValueError):
        return False
    return not math.isnan(r)


def analyze(df, flow_data=None, std_data=None, rsi=None, hour=None):
    """
    Estrategia VFA (Volume Flow Analysis)
    Version V5 — Filtros P0/P1 del Informe Estrategia V5
    """
    try:
        current_time = time.time()

        # 1. Validacion de datos de flujo
        if not flow_data:
            return {'action': None, 'message': "WAIT: Recopilando datos de volumen..."}

        # --- A. PROCESAMIENTO DE VOLUMEN (MONEY FLOW) ---
        call_vol       = 0.0
        put_vol        = 0.0
        whale_detected = None
        whale_amount   = 0.0

        recent_flows = [f for f in flow_data if current_time - f['timestamp'] <= LOOKBACK_SECONDS]

        for flow in recent_flows:
            try:
                bet   = float(flow['bet'])
                trend = flow['trend']
                if trend == 'call':
                    call_vol += bet
                elif trend == 'put':
                    put_vol += bet
                if bet >= WHALE_THRESHOLD:
                    whale_detected = trend
                    whale_amount   = bet
            except (ValueError, KeyError):
                continue

        if put_vol == 0:
            money_ratio = 100.0 if call_vol > 0 else 1.0
        else:
            money_ratio = call_vol / put_vol

        # --- B. PROCESAMIENTO DE VOLATILIDAD (STD) ---
        current_std          = 0.0
        avg_std              = 0.0
        is_volatility_rising = False

        if std_data:
            recent_stds = [s for s in std_data if current_time - s['timestamp'] <= LOOKBACK_SECONDS]
            if recent_stds:
                try:
                    values = []
                    for x in recent_stds:
                        try:
                            values.append(float(x['std']))
                        except (ValueError, TypeError, KeyError):
                            continue
                    if values:
                        current_std          = float(values[-1])
                        avg_std              = float(sum(values) / len(values))
                        is_volatility_rising = current_std >= avg_std
                except Exception:
                    pass

        # Fallback: STD calculada desde el DataFrame si std_data no aportó valor
        if current_std == 0.0 and df is not None and len(df) >= 5:
            try:
                current_std = float(df['close'].pct_change().std())
            except Exception:
                pass

        # --- C. TELEMETRIA (unico helper; is_whale se conoce aqui) ---
        is_whale = whale_detected is not None

        def _telemetry():
            return {
                'ratio':          money_ratio,
                'call_vol':       call_vol,
                'call_volume':    call_vol,
                'put_vol':        put_vol,
                'put_volume':     put_vol,
                'std':            current_std,
                'std_current':    current_std,
                'std_avg':        avg_std,
                'whale':          is_whale,
                'whale_detected': is_whale,
                'whale_amount':   whale_amount,
            }

        # --- D. FILTROS V5 (P0 primero, luego P1) ---

        # REGLA 1 (P0): Filtro horario estricto
        if datetime.utcnow().hour not in HORAS_PERMITIDAS:
            return {
                'action':    None,
                'message':   'WAIT: Hora no autorizada',
                'telemetry': _telemetry(),
            }

        # REGLA 4a (P1): Volatilidad minima absoluta
        if current_std < STD_MIN:
            return {
                'action':    None,
                'message':   'WAIT: Volatilidad muerta',
                'telemetry': _telemetry(),
            }

        # REGLA 2 (P1): Trigger exclusivo de ballenas
        if not is_whale:
            return {
                'action':    None,
                'message':   'WAIT: Solo operar con trigger whale',
                'telemetry': _telemetry(),
            }

        # REGLA 3 (P0): Rango seguro de ballenas
        if not (WHALE_AMT_MIN <= whale_amount <= WHALE_AMT_MAX):
            return {
                'action':    None,
                'message':   'WAIT: Whale fuera de rango seguro (28M-999M)',
                'telemetry': _telemetry(),
            }

        # --- E. VALIDACIONES ADICIONALES (solo trades con whale valida) ---

        # RSI: valida ambas direcciones
        if _rsi_defined(rsi):
            r = float(rsi)
            if whale_detected == 'call' and r < 45:
                return {
                    'action':    None,
                    'message':   'WAIT: RSI bajo para CALL',
                    'telemetry': _telemetry(),
                }
            if whale_detected == 'put' and (r < 45 or r > 60):
                return {
                    'action':    None,
                    'message':   'WAIT: RSI fuera de zona PUT (45-60)',
                    'telemetry': _telemetry(),
                }

        # REGLA 4b (P1): Volatilidad excesiva para PUT
        if whale_detected == 'put' and current_std > STD_MAX_PUT:
            return {
                'action':    None,
                'message':   'WAIT: Volatilidad excesiva para PUT',
                'telemetry': _telemetry(),
            }

        # --- F. SEÑAL DE SALIDA ---
        msg = f"WHALE: Apuesta de {whale_detected.upper()} de {whale_amount:.0f}"
        return {
            'action':    whale_detected,
            'message':   msg,
            'telemetry': _telemetry(),
        }

    except Exception as e:
        return {'action': None, 'message': f"ERROR VFA: {str(e)}"}