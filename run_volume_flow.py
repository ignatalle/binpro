"""
Script para ejecutar el bot con la estrategia Volume Flow Analysis

Esta estrategia utiliza datos ocultos del WebSocket de Binomo:
- social_trading_deal: Flujo real de dinero (bet, trend)
- quotes_range: Volatilidad algorítmica (std)

Características:
1. Money Flow: Detecta desequilibrios de flujo de capital
2. Whale Tracker: Sigue apuestas institucionales grandes
3. Volatility Confirmation: Opera solo cuando volatilidad aumenta
4. Absorption Detector: Predice breakouts por acumulación
5. Gemini Logger: Telemetría para auditoría por IA

Uso:
    python run_volume_flow.py
"""

import asyncio
import strategies.volume_flow as strategy_module
from main_bot import ProfitReaperBotV27

# ============================================================================
# CONFIGURACIÓN DEL BOT
# ============================================================================

# Credenciales (IMPORTANTE: Usa tus propias credenciales)
AUTHTOKEN = "TU_AUTHTOKEN_AQUI"
DEVICE_ID = "TU_DEVICE_ID_AQUI"

# Tipo de cuenta
ACCOUNT_TYPE = "demo"  # Cambiar a "real" para operar con dinero real

# Activo a operar
ASSET_RIC = "Z-CRY/IDX"  # Crypto Index de Binomo

# Timeframe
TIMEFRAME = 60  # 60 segundos (1 minuto) o 30 segundos

# ============================================================================
# PARÁMETROS DE LA ESTRATEGIA VOLUME FLOW
# ============================================================================
# 
# Los parámetros se configuran en strategies/volume_flow.py:
#
# - LOOKBACK_SECONDS = 15        # Ventana de análisis
# - RATIO_CALL_THRESHOLD = 2.5   # Threshold para CALL
# - RATIO_PUT_THRESHOLD = 0.4    # Threshold para PUT
# - WHALE_THRESHOLD = 5,000,000  # Detectar ballenas > $5M
# - MIN_VOLUME = 100,000         # Volumen mínimo para operar
#
# Edita ese archivo para ajustar según tu mercado.
#
# ============================================================================

def main():
    """Función principal para iniciar el bot"""
    
    # Validar credenciales
    if AUTHTOKEN == "TU_AUTHTOKEN_AQUI" or DEVICE_ID == "TU_DEVICE_ID_AQUI":
        print("=" * 70)
        print("⚠️  ERROR: Debes configurar tus credenciales primero")
        print("=" * 70)
        print("\nEdita este archivo (run_volume_flow.py) y reemplaza:")
        print("  - AUTHTOKEN: Tu token de autenticación de Binomo")
        print("  - DEVICE_ID: Tu ID de dispositivo")
        print("\nPuedes obtenerlos desde:")
        print("  1. Abre las DevTools del navegador (F12)")
        print("  2. Ve a la pestaña Network")
        print("  3. Inicia sesión en Binomo")
        print("  4. Busca las peticiones WebSocket")
        print("  5. Copia el authtoken y device_id de los headers/params")
        print("=" * 70)
        return
    
    # Mostrar configuración
    print("\n" + "=" * 70)
    print("🚀 INICIANDO BOT CON VOLUME FLOW ANALYSIS")
    print("=" * 70)
    print(f"📊 Estrategia: Volume Flow Analysis (VFA)")
    print(f"💰 Cuenta: {ACCOUNT_TYPE.upper()}")
    print(f"📈 Activo: {ASSET_RIC}")
    print(f"⏱️  Timeframe: {TIMEFRAME} segundos")
    print("=" * 70)
    print("\n🔍 Datos Capturados del WebSocket:")
    print("  ✓ social_trading_deal → Flujo de dinero real (bet, trend)")
    print("  ✓ quotes_range → Volatilidad algorítmica (std)")
    print("\n🎯 Tipos de Señales:")
    print("  📈 Money Flow: Desequilibrio de flujo (CALL/PUT ratio)")
    print("  🐋 Whale: Apuestas institucionales > $5M")
    print("  💎 Absorption: Alto volumen sin movimiento → Breakout")
    print("\n🧠 Gemini Logger:")
    print("  ✓ Telemetría guardada en: gemini_analysis_log.csv")
    print("  ✓ Auditoría por IA: Gemini, GPT, Claude")
    print("=" * 70)
    print("\n⏳ Iniciando conexión...\n")
    
    # Crear instancia del bot
    bot = ProfitReaperBotV27(
        authtoken=AUTHTOKEN,
        device_id=DEVICE_ID,
        account_type=ACCOUNT_TYPE,
        asset_ric=ASSET_RIC,
        strategy_module=strategy_module,
        timeframe_seconds=TIMEFRAME
    )
    
    # Iniciar el bot (asíncrono)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n\n" + "=" * 70)
        print("🛑 Bot detenido por el usuario (Ctrl+C)")
        print("=" * 70)
        print("\n📊 Archivos generados:")
        print("  ✓ historial_trading.csv - Historial de operaciones")
        print("  ✓ gemini_analysis_log.csv - Telemetría para IA")
        print("  ✓ debug_trading.log - Log detallado de debug")
        print("  ✓ memory_Z_CRY_IDX.csv - Velas históricas guardadas")
        print("\n🧠 Próximos pasos:")
        print("  1. Analiza gemini_analysis_log.csv con una IA")
        print("  2. Identifica patrones de éxito/fracaso")
        print("  3. Optimiza parámetros en strategies/volume_flow.py")
        print("=" * 70 + "\n")
    except Exception as e:
        print("\n\n" + "=" * 70)
        print(f"❌ ERROR CRÍTICO: {str(e)}")
        print("=" * 70)
        print("\nRevisa debug_trading.log para más detalles")
        import traceback
        print("\nTraceback completo:")
        print(traceback.format_exc())
        print("=" * 70 + "\n")

if __name__ == "__main__":
    main()
