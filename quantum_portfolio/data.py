"""Data layer: ticker parsing, Yahoo Finance downloads, validation, statistics."""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
import yfinance as yf

from quantum_portfolio import MIN_HISTORY_DAYS, TRADING_DAYS


def parse_tickers(text: str) -> list[str]:
    """Split free-form input (commas, semicolons, spaces or newlines) into a
    deduplicated, uppercased ticker list — long lists are usually pasted in
    one-per-line, so every common separator is accepted."""
    seen: set[str] = set()
    tickers: list[str] = []
    for part in re.split(r"[,;\s]+", text):
        ticker = part.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def download_close(tickers: tuple[str, ...]) -> pd.DataFrame:
    """Download 1 year of daily (adjusted) closes for any number of tickers.
    yfinance batches the request internally; ~500 tickers is one bulk call."""
    raw = yf.download(
        list(tickers),
        period="1y",
        interval="1d",
        auto_adjust=True,   # adjust for splits/dividends
        progress=False,
        group_by="column",
    )
    if raw is None or len(raw) == 0:
        raise RuntimeError("Yahoo Finance returned no data for these tickers.")
    return extract_close(raw, list(tickers))


def extract_close(raw: pd.DataFrame | pd.Series, tickers: list[str]) -> pd.DataFrame:
    """Return a plain DataFrame of closing prices (one column per ticker).

    yfinance returns different shapes depending on version and ticker count:
    MultiIndex (Price, Ticker) columns, flat OHLCV columns, or a bare Series.
    """
    if isinstance(raw, pd.Series):
        return raw.to_frame(name=tickers[0]).copy()
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            raise ValueError(
                "Unrecognized price data format from yfinance "
                f"(no 'Close' level in columns: {list(raw.columns)[:8]}…)"
            )
        close = raw["Close"]
        if isinstance(close, pd.Series):  # single ticker under a MultiIndex
            close = close.to_frame(name=tickers[0])
        return close.copy()
    if "Close" in raw.columns:  # flat single-ticker frame
        return raw[["Close"]].rename(columns={"Close": tickers[0]}).copy()
    # Fail loudly on an unrecognized layout rather than silently mislabeling
    # every ticker as invalid downstream.
    raise ValueError(
        f"Unrecognized price data format from yfinance (columns: {list(raw.columns)})"
    )


def validate_prices(
    close: pd.DataFrame, tickers: list[str]
) -> tuple[pd.DataFrame, list[str], list[str], list[str]]:
    """Classify tickers and align the survivors on common trading days.

    Returns (aligned_closes, valid, invalid, short) where `invalid` tickers had
    no data at all and `short` ones had fewer than MIN_HISTORY_DAYS prices
    (e.g. a recent IPO) — kept separate so each gets an accurate warning, and
    excluded so one short series can't truncate everyone's overlap window.
    """
    close = close.reindex(columns=list(tickers))
    counts = close.notna().sum()
    invalid = [t for t in tickers if counts[t] == 0]
    short = [t for t in tickers if 0 < counts[t] < MIN_HISTORY_DAYS]
    valid = [t for t in tickers if counts[t] >= MIN_HISTORY_DAYS]
    aligned = close[valid].dropna()  # keep only days where every market traded
    return aligned, valid, invalid, short


def annualized_stats(close: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Annualized mean-return vector μ and covariance matrix Σ from daily closes."""
    returns = close.pct_change(fill_method=None).dropna()
    mu = returns.mean().to_numpy() * TRADING_DAYS
    sigma = returns.cov().to_numpy() * TRADING_DAYS
    # Numerical guard: with many assets and ~250 observations Σ is rank-deficient
    # by construction — nudge it onto the positive-semidefinite cone.
    min_eig = float(np.linalg.eigvalsh(sigma).min())
    if min_eig < 1e-10:
        sigma = sigma + (1e-8 - min(min_eig, 0.0)) * np.eye(len(mu))
    return mu, sigma
