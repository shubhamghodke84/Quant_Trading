#!/usr/bin/env python3
"""
Convert MT5 History Center CSV export to backtest engine format.

MT5 exports bars with separate Date and Time columns:
    <DATE>       <TIME>    <OPEN>   <HIGH>   <LOW>    <CLOSE>  <TICKVOL>  <VOL>  <SPREAD>
    2025.01.02   00:05:00  2062.50  2063.20  2061.80  2062.90  1234       0      5

This script merges Date+Time into a single 'timestamp' column and saves as:
    timestamp, open, high, low, close, volume

Usage:
    python scripts/convert_mt5_export.py --input XAUUSD_M5.csv
    python scripts/convert_mt5_export.py --input XAUUSD_M5.csv --out data/historical/XAUUSD_5m_real.csv
    python scripts/convert_mt5_export.py --input XAUUSD_M5.csv --symbol XAUUSD --interval 5m
"""

import sys
import argparse
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def detect_mt5_format(df: pd.DataFrame) -> str:
    """Detect MT5 export column format variant."""
    cols = [c.strip().upper().lstrip('<').rstrip('>') for c in df.columns]

    # Variant A: <DATE> <TIME> <OPEN> ...
    if 'DATE' in cols and 'TIME' in cols:
        return 'datetime_split'

    # Variant B: combined datetime in first column
    if len(cols) >= 5:
        first_col = str(df.iloc[0, 0])
        if ' ' in first_col or 'T' in first_col:
            return 'datetime_combined'

    return 'unknown'


def parse_mt5_csv(path: Path) -> pd.DataFrame:
    """Parse MT5 exported CSV regardless of delimiter/format variant."""
    # Try tab-separated first (MT5 default), then comma
    for sep in ('\t', ',', ';'):
        try:
            df = pd.read_csv(path, sep=sep, header=0)
            if df.shape[1] >= 6:
                break
        except Exception:
            continue
    else:
        raise ValueError(f"Could not parse {path} — try exporting as CSV with tab or comma separator")

    # Normalise column names: strip angle brackets and whitespace
    df.columns = [c.strip().lstrip('<').rstrip('>').upper() for c in df.columns]

    fmt = detect_mt5_format(df)

    if fmt == 'datetime_split':
        # Merge DATE and TIME columns with explicit format (avoids slow per-row inference)
        df['timestamp'] = pd.to_datetime(
            df['DATE'].astype(str) + ' ' + df['TIME'].astype(str),
            format='%Y.%m.%d %H:%M:%S',
        )
    elif fmt == 'datetime_combined':
        first_col = df.columns[0]
        df['timestamp'] = pd.to_datetime(df[first_col], format='%Y.%m.%d %H:%M:%S')
    else:
        raise ValueError(
            "Unknown MT5 format. Expected columns: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL>"
        )

    # Map column names to standard names
    col_map = {
        'OPEN':    'open',
        'HIGH':    'high',
        'LOW':     'low',
        'CLOSE':   'close',
        'TICKVOL': 'volume',
        'VOL':     'volume',   # fallback
        'VOLUME':  'volume',
    }

    # Rename only columns that exist
    rename = {k: v for k, v in col_map.items() if k in df.columns}
    df = df.rename(columns=rename)

    required = ['open', 'high', 'low', 'close']
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns after rename: {missing}\nGot: {list(df.columns)}")

    # Fill volume with 0 if absent
    if 'volume' not in df.columns:
        df['volume'] = 0

    out = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].copy()
    out = out.dropna(subset=['open', 'high', 'low', 'close'])
    # Skip sort if already ordered (MT5 exports are typically pre-sorted)
    if not out['timestamp'].is_monotonic_increasing:
        out = out.sort_values('timestamp')
    out = out.reset_index(drop=True)
    return out


def main():
    parser = argparse.ArgumentParser(description="Convert MT5 History Center CSV to backtest format")
    parser.add_argument("--input", required=True, help="Path to MT5-exported CSV file")
    parser.add_argument("--symbol", default="XAUUSD", help="Symbol name (default: XAUUSD)")
    parser.add_argument("--interval", default="5m", help="Timeframe string, e.g. 5m, 1h (default: 5m)")
    parser.add_argument("--out", default=None,
                        help="Output CSV path (auto-derived from symbol/interval if omitted)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"❌ File not found: {input_path}")
        sys.exit(1)

    out_path = Path(args.out) if args.out else (
        PROJECT_ROOT / "data" / "historical" / f"{args.symbol}_{args.interval}_real.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Reading: {input_path}")
    try:
        df = parse_mt5_csv(input_path)
    except Exception as e:
        print(f"❌ Parse error: {e}")
        sys.exit(1)

    df.to_csv(out_path, index=False)

    print(f"✅ Converted {len(df):,} bars → {out_path}")
    print(f"   Date range : {df['timestamp'].min()} → {df['timestamp'].max()}")
    print(f"   Price range: {df['close'].min():.2f} → {df['close'].max():.2f}")


if __name__ == "__main__":
    main()
