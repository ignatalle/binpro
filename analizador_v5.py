#!/usr/bin/env python3
"""
analizador_v5.py — Auditoría en tiempo real del bot ProfitReaper V27
=====================================================================
Lee gemini_analysis_log.csv en modo seguro (BytesIO) sin bloquear
al bot principal que sigue escribiendo en el mismo archivo.

Uso:
    python analizador_v5.py                  # snapshot único
    python analizador_v5.py --loop           # refresca cada 60 s
    python analizador_v5.py --bet 65         # cambia apuesta base (default: 10)
    python analizador_v5.py --csv otra_ruta.csv
"""

import sys
import io
import time
import argparse
from datetime import datetime
from pathlib import Path

# Forzar UTF-8 en la consola de Windows para que los simbolos Unicode se muestren correctamente
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass  # Python < 3.7, no aplica

import pandas as pd


# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
CSV_PATH  = "gemini_analysis_log.csv"
BET_BASE  = 10.0     # apuesta base en $ (ajustable con --bet)
BREAKEVEN = 54.02    # WR mínimo para payout ~85 %
W         = 72       # ancho interior del dashboard (entre los ║)


# ─── LECTURA SEGURA ───────────────────────────────────────────────────────────
def _load_csv_safe(path: str) -> pd.DataFrame:
    """
    Lee el CSV completo en binario hacia un buffer en memoria (BytesIO)
    y lo parsea desde ahí. Esto evita interferir con el lock de escritura
    del bot principal en Windows y no bloquea el archivo.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"No se encontró '{path}'.")
    if p.stat().st_size == 0:
        raise ValueError("El archivo CSV está vacío.")
    with open(p, "rb") as f:
        raw = f.read()
    return pd.read_csv(
        io.BytesIO(raw),
        encoding="utf-8",
        on_bad_lines="skip",   # tolera filas incompletas sin crashear
    )


# ─── UTILIDADES VISUALES ──────────────────────────────────────────────────────
def _bar(wr: float, width: int = 14) -> str:
    """Barra de progreso proporcional al Win Rate."""
    filled = int(round(wr / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _wr_tag(wr: float, n: int) -> str:
    """Etiqueta semáforo para cada hora."""
    if n < 3:
        return "muestra insuf."
    if wr >= 70.0:
        return "⭐ DORADA     "
    if wr >= 60.0:
        return "✅ BUENA      "
    if wr >= BREAKEVEN:
        return "⚠️  MARGINAL   "
    return "❌ ZONA MUERTE"


def _row(label: str, value: str) -> str:
    """Línea de tabla con alineación fija."""
    inner = f"  {label:<40}{value}"
    pad   = W - len(inner)
    return f"║{inner}{' ' * max(pad, 0)}║"


# ─── MOTOR DEL DASHBOARD ──────────────────────────────────────────────────────
def render_dashboard(df_all: pd.DataFrame, bet: float) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

    # --- 1. Filtros principales ---
    df_trades = df_all[df_all["decision"].isin(["CALL", "PUT"])].copy()
    df_res    = df_trades[df_trades["resultado"].isin(["WON", "LOST"])].copy()

    df_res["profit_real"] = pd.to_numeric(
        df_res["profit_real"], errors="coerce"
    ).fillna(0.0)

    total_signals = len(df_all)
    total_trades  = len(df_trades)
    total_res     = len(df_res)
    won           = int((df_res["resultado"] == "WON").sum())
    lost          = int((df_res["resultado"] == "LOST").sum())
    pending       = total_trades - total_res

    wr = won / total_res * 100 if total_res > 0 else 0.0

    # --- 2. PnL neto ---
    # profit_real es el retorno BRUTO (apuesta devuelta + ganancia pura).
    # Para obtener la ganancia NETA de los WON hay que descontarles la apuesta:
    #   ganancia_neta_won = suma(profit_real) - (won * bet)
    # La pérdida de los LOST es la apuesta que no se devuelve:
    #   perdida_lost = lost * bet
    sum_profit_real = float(df_res.loc[df_res["resultado"] == "WON", "profit_real"].sum())
    net_won    = sum_profit_real - (won * bet)   # solo la ganancia pura por WON
    gross_lost = lost * bet                      # capital perdido en LOST
    pnl_neto   = net_won - gross_lost
    pnl_prefix = "+" if pnl_neto >= 0 else ""

    # --- 3. Desglose horario (extrae hora de "2026-04-02 12:31:03.513") ---
    try:
        df_res = df_res.copy()
        df_res["hora"] = (
            df_res["timestamp"].astype(str).str[11:13]
        )
        df_res["hora"] = pd.to_numeric(df_res["hora"], errors="coerce")
        df_res = df_res.dropna(subset=["hora"])
        df_res["hora"] = df_res["hora"].astype(int)

        hourly = (
            df_res.groupby("hora")
            .agg(
                n  =("resultado", "count"),
                w  =("resultado", lambda x: (x == "WON").sum()),
            )
            .reset_index()
        )
        hourly["wr_h"]  = hourly["w"] / hourly["n"] * 100
        hourly["l"]     = hourly["n"] - hourly["w"]
        hourly_ok = True
    except Exception:
        hourly_ok = False

    # ─── RENDER ──────────────────────────────────────────────────────────────
    SEP     = "═" * W
    SEPDASH = "─" * W
    SEPDOT  = "·" * W

    wr_icon   = "✓" if wr >= BREAKEVEN else "✗"
    wr_desc   = "sobre" if wr >= BREAKEVEN else "bajo "
    wr_status = f"{wr:6.2f} %  {wr_icon} {wr_desc} breakeven ({BREAKEVEN} %)"

    print()
    print(f"╔{SEP}╗")
    # Título
    title = "📊  AUDITORÍA EN TIEMPO REAL — ProfitReaper V27"
    print(f"║  {title:<{W-4}}  ║")
    sub = f"Leído: {now_str}  |  Apuesta base fija: ${bet:.2f}"
    print(f"║  {sub:<{W-4}}  ║")

    # ── Bloque operaciones ──
    print(f"╠{SEP}╣")
    print(_row("Señales totales (CALL + PUT + WAIT)", f"{total_signals:>7,d}"))
    print(_row("Operaciones ejecutadas (CALL + PUT)",  f"{total_trades:>7,d}"))
    print(_row("  └─ Resueltas (WON / LOST)",          f"{total_res:>7,d}"))
    print(_row("  └─ Pendientes (sin resultado aún)",  f"{pending:>7,d}"))

    # ── Bloque resultados ──
    print(f"╠{SEP}╣")
    print(_row("✅  WON",                               f"{won:>7,d}"))
    print(_row("❌  LOST",                              f"{lost:>7,d}"))
    print(_row("Win Rate global",                       wr_status))

    # ── Bloque PnL ──
    print(f"╠{SEP}╣")
    print(f"║  {'PnL NETO  (apuesta base = ${:.2f} por operación)'.format(bet):<{W-4}}  ║")
    print(f"║  {SEPDASH:<{W-4}}  ║")
    print(_row(f"  Ganancia neta WON  (profit_real − apuesta × {won})", f"+${net_won:>10,.2f}"))
    print(_row(f"  Pérdida LOST       (${bet:.0f} × {lost} ops)",        f"-${gross_lost:>10,.2f}"))
    print(f"║  {SEPDOT:<{W-4}}  ║")

    # Fila balance con énfasis
    balance_label = "  BALANCE NETO"
    balance_value = f"{pnl_prefix}${pnl_neto:,.2f}"
    balance_inner = f"{balance_label:<42}{balance_value}"
    pad = W - len(balance_inner)
    print(f"║{balance_inner}{' ' * max(pad, 0)}║")

    # ── Bloque horario ──
    print(f"╠{SEP}╣")
    print(f"║  {'DESGLOSE POR HORA UTC':<{W-4}}  ║")

    if hourly_ok and len(hourly) > 0:
        header = f"  {'Hora':<6}│ {'WR':>7}   │ {'Barra':16} │ {'N':>4} │ {'W':>4} │ {'L':>4} │ Estado"
        print(f"║  {'-'*62:<{W-4}}  ║")
        print(f"║{header:<{W}}║")
        print(f"║  {'-'*62:<{W-4}}  ║")

        for _, r in hourly.sort_values("hora").iterrows():
            h     = int(r["hora"])
            n     = int(r["n"])
            w     = int(r["w"])
            l     = int(r["l"])
            wr_h  = float(r["wr_h"])
            bar   = _bar(wr_h, 14)
            tag   = _wr_tag(wr_h, n)
            line  = f"  H{h:02d}UTC │ {wr_h:6.1f} %  │ {bar} │ {n:4d} │ {w:4d} │ {l:4d} │ {tag}"
            pad   = W - len(line)
            print(f"║{line}{' ' * max(pad, 0)}║")
    else:
        msg = "  Sin operaciones resueltas para calcular desglose horario."
        print(f"║{msg:<{W}}║")

    print(f"╚{SEP}╝")
    print()


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auditoría en tiempo real del bot ProfitReaper V27",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--csv",  default=CSV_PATH, help="Ruta al CSV (default: gemini_analysis_log.csv)")
    parser.add_argument("--bet",  default=BET_BASE, type=float, help="Apuesta base en $ (default: 10)")
    parser.add_argument("--loop", action="store_true", help="Refresca el dashboard cada 60 segundos")
    args = parser.parse_args()

    while True:
        try:
            df = _load_csv_safe(args.csv)
            render_dashboard(df, args.bet)

        except FileNotFoundError as e:
            print(f"\n  [ERROR] {e}")
            print("  Verifica que el bot haya corrido al menos una vez y generado el CSV.\n")
            sys.exit(1)

        except ValueError as e:
            print(f"\n  [AVISO] {e}\n")
            sys.exit(0)

        except PermissionError:
            print("\n  [AVISO] Archivo temporalmente bloqueado. Reintentando en 5 s...")
            time.sleep(5)
            continue

        except Exception as e:
            print(f"\n  [ERROR inesperado] {type(e).__name__}: {e}\n")
            sys.exit(1)

        if not args.loop:
            break

        try:
            print("  Próximo refresco en 60 s — Ctrl+C para salir.\n")
            time.sleep(60)
        except KeyboardInterrupt:
            print("\n  Auditoría detenida.\n")
            break


if __name__ == "__main__":
    main()
