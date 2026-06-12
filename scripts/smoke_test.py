"""Headless smoke test for the Quantum Portfolio Optimizer.

Exercises the full pipeline without a browser and (almost) without network:
  * ticker parsing and yfinance response-shape handling
  * validation (invalid / short-history tickers) and annualized statistics
  * classical screening of a 500-stock universe
  * the hierarchical tournament with both classical and QAOA chunk solvers
  * the timeout -> classical fallback path and the weighting stage
  * IBM engine construction errors (no network needed)
  * (optional) a live yfinance download — skipped gracefully if offline

Run with:  python scripts/smoke_test.py
"""

import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quantum_portfolio import MIN_HISTORY_DAYS  # noqa: E402
from quantum_portfolio import data, engines, optimizer  # noqa: E402

FAILURES = []


def check(name, fn):
    t0 = time.perf_counter()
    try:
        fn()
        print(f"PASS  {name}  ({time.perf_counter() - t0:.2f}s)")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL  {name}: {exc!r}")


def synthetic_market(n=4, seed=11):
    """A synthetic market of n assets with a PSD covariance matrix."""
    rng = np.random.default_rng(seed)
    mu = rng.normal(0.10, 0.15, size=n)
    a = rng.normal(size=(n, max(4, n)))
    sigma = 0.05 * (a @ a.T) / a.shape[1] + 0.01 * np.eye(n)
    return mu, sigma


def local_engine(shots=256, seed=7):
    return engines.build_local_engine(shots=shots, seed=seed)


# --- data layer ---------------------------------------------------------------

def test_parse_tickers():
    text = "aapl, MSFT;nvda\nSAP.DE  aapl\n\n msft,"
    assert data.parse_tickers(text) == ["AAPL", "MSFT", "NVDA", "SAP.DE"]


def test_extract_close_multiindex():
    idx = pd.date_range("2025-01-01", periods=5, freq="B")
    cols = pd.MultiIndex.from_product([["Close", "Open"], ["AAPL", "MSFT"]],
                                      names=["Price", "Ticker"])
    raw = pd.DataFrame(np.arange(20.0).reshape(5, 4), index=idx, columns=cols)
    close = data.extract_close(raw, ["AAPL", "MSFT"])
    assert list(close.columns) == ["AAPL", "MSFT"] and close.shape == (5, 2)


def test_extract_close_flat_series_and_unknown():
    idx = pd.date_range("2025-01-01", periods=5, freq="B")
    flat = pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5,
                         "Close": 1.5, "Volume": 100}, index=idx)
    assert list(data.extract_close(flat, ["AAPL"]).columns) == ["AAPL"]
    series = pd.Series(np.linspace(1, 2, 5), index=idx)
    assert list(data.extract_close(series, ["MSFT"]).columns) == ["MSFT"]
    try:
        data.extract_close(pd.DataFrame({"Foo": [1.0]}), ["AAPL"])
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unrecognized data shape")


def test_validate_prices():
    periods = MIN_HISTORY_DAYS + 20
    idx = pd.date_range("2025-01-01", periods=periods, freq="B")
    close = pd.DataFrame(
        {
            "AAPL": np.linspace(100, 110, periods),
            "BAD": np.nan,
            "MSFT": np.linspace(50, 55, periods),
            "NEWIPO": [np.nan] * MIN_HISTORY_DAYS + list(np.linspace(10, 11, 20)),
        },
        index=idx,
    )
    aligned, valid, invalid, short = data.validate_prices(
        close, ["AAPL", "BAD", "MSFT", "NEWIPO"]
    )
    assert valid == ["AAPL", "MSFT"] and invalid == ["BAD"] and short == ["NEWIPO"]
    assert aligned.shape == (periods, 2)  # short ticker must not truncate others


def test_annualized_stats_rank_deficient():
    # 80 assets from only 70 days of data: covariance is rank-deficient by
    # construction — the PSD guard must still produce a usable matrix.
    rng = np.random.default_rng(3)
    idx = pd.date_range("2025-01-01", periods=70, freq="B")
    prices = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, size=(70, 80)), axis=0)),
        index=idx, columns=[f"S{i}" for i in range(80)],
    )
    mu, sigma = data.annualized_stats(prices)
    assert mu.shape == (80,) and sigma.shape == (80, 80)
    assert np.linalg.eigvalsh(sigma).min() >= 0


# --- optimizer: screening & tournament -----------------------------------------

def test_screen_universe():
    mu, sigma = synthetic_market(200, seed=5)
    pool = optimizer.screen_universe(mu, sigma, q=1.0, pool_size=32)
    assert pool.shape == (32,) and len(set(pool)) == 32
    score = mu - np.diag(sigma)
    assert min(score[pool]) >= np.sort(score)[::-1][31] - 1e-12  # truly the top 32


def test_classical_chunk_select_exact_and_greedy():
    mu, sigma = synthetic_market(10, seed=2)
    mask = optimizer.classical_chunk_select(mu, sigma, q=1.0, k=4)
    assert mask.sum() == 4
    mu2, sigma2 = synthetic_market(25, seed=4)  # C(25,12) > limit -> greedy path
    mask2 = optimizer.classical_chunk_select(mu2, sigma2, q=1.0, k=12)
    assert mask2.sum() == 12


def test_tournament_500_stocks_classical():
    mu, sigma = synthetic_market(500, seed=9)
    sel, stats = optimizer.tournament_select(
        mu, sigma, q=1.0, budget=12, chunk_size=12,
        chunk_solver=optimizer.classical_chunk_select, seed=1,
    )
    assert sel.sum() == 12, sel.sum()
    assert stats["subproblems"] > 10 and stats["max_chunk"] <= 12


def test_tournament_budget_near_pool():
    mu, sigma = synthetic_market(30, seed=12)
    sel, stats = optimizer.tournament_select(
        mu, sigma, q=1.0, budget=20, chunk_size=8,
        chunk_solver=optimizer.classical_chunk_select, seed=1,
    )
    assert sel.sum() == 20, sel.sum()  # proportional path must hit it exactly
    assert stats["max_chunk"] <= 8


def test_tournament_single_chunk_and_hold_all():
    mu, sigma = synthetic_market(10, seed=13)
    sel, _ = optimizer.tournament_select(
        mu, sigma, 1.0, 4, 16, optimizer.classical_chunk_select
    )
    assert sel.sum() == 4
    sel_all, _ = optimizer.tournament_select(
        mu, sigma, 1.0, 10, 16, optimizer.classical_chunk_select
    )
    assert sel_all.sum() == 10


# --- optimizer: quantum path ----------------------------------------------------

def test_qaoa_chunk_select():
    mu, sigma = synthetic_market(4, seed=11)
    sel = optimizer.qaoa_chunk_select(
        mu, sigma, q=1.0, k=2, engine=local_engine(),
        reps=1, maxiter=40, seed=7,
    )
    assert sel.sum() == 2 and set(np.unique(sel)) <= {0, 1}


def test_optimize_portfolio_quantum_tournament():
    mu, sigma = synthetic_market(40, seed=21)
    res = optimizer.optimize_portfolio(
        mu, sigma, q=1.0, budget=5, engine=local_engine(),
        pool_size=16, chunk_size=6, reps=1, maxiter=30,
        timeout_s=300, seed=7,
    )
    assert res["method"] == "quantum", (res["method"], res["fallback_reason"])
    assert res["selection"].sum() == 5
    assert abs(res["weights"].sum() - 1.0) < 1e-6
    assert res["screened"] and len(res["pool_indices"]) == 16
    assert res["stats"]["subproblems"] >= 2 and res["stats"]["max_chunk"] <= 6


def test_timeout_falls_back_to_classical():
    mu, sigma = synthetic_market(20, seed=22)
    res = optimizer.optimize_portfolio(
        mu, sigma, q=1.0, budget=4, engine=local_engine(),
        pool_size=16, chunk_size=8, reps=1, maxiter=30,
        timeout_s=0.001, seed=7,
    )
    assert res["method"] == "classical", res["method"]
    assert "time budget" in (res["fallback_reason"] or "")
    assert res["selection"].sum() == 4
    # Give the abandoned QAOA worker a moment to finish, so it doesn't get
    # torn down mid-COBYLA at interpreter exit (cosmetic stderr noise).
    time.sleep(3)


# --- weighting & engines --------------------------------------------------------

def test_weights():
    mu, sigma = synthetic_market(6, seed=2)
    w = optimizer.slsqp_weights(mu, sigma, q=1.0)
    assert abs(w.sum() - 1.0) < 1e-6 and (w >= -1e-9).all()
    equal = np.full(6, 1 / 6)
    utility = lambda v: v @ mu - v @ sigma @ v  # noqa: E731
    assert utility(w) >= utility(equal) - 1e-9
    mv = optimizer.min_variance_weights(sigma)
    assert abs(mv.sum() - 1.0) < 1e-6 and (mv >= -1e-9).all()


def test_ibm_engine_errors_cleanly_without_token():
    os.environ.pop("IBM_QUANTUM_TOKEN", None)
    assert engines.resolve_ibm_token("") is None
    assert engines.resolve_ibm_token("  abc  ") == "abc"
    os.environ["IBM_QUANTUM_TOKEN"] = "from-env"
    assert engines.resolve_ibm_token(None) == "from-env"
    os.environ.pop("IBM_QUANTUM_TOKEN", None)
    try:
        engines.build_ibm_engine(None, None, None, shots=1024, min_qubits=8)
    except ValueError as exc:
        assert "API key" in str(exc)
        return
    raise AssertionError("expected ValueError without a token")


def test_random_portfolios():
    mu, sigma = synthetic_market(12, seed=2)
    rets, vols = optimizer.random_portfolios(mu, sigma, 500, 42)
    assert rets.shape == (500,) and vols.shape == (500,) and (vols > 0).all()


def test_live_yfinance_optional():
    import yfinance as yf

    try:
        raw = yf.download("AAPL", period="5d", interval="1d",
                          auto_adjust=True, progress=False)
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP  live yfinance fetch (network unavailable: {exc!r})")
        return
    if raw is None or len(raw) == 0:
        print("SKIP  live yfinance fetch (empty response — likely rate-limited)")
        return
    close = data.extract_close(raw, ["AAPL"])
    assert "AAPL" in close.columns and len(close) > 0
    print(f"      live fetch OK: {len(close)} rows of AAPL closes")


if __name__ == "__main__":
    check("parse_tickers: mixed separators", test_parse_tickers)
    check("extract_close: MultiIndex columns", test_extract_close_multiindex)
    check("extract_close: flat/Series/unknown shapes", test_extract_close_flat_series_and_unknown)
    check("validate_prices: invalid & short-history tickers", test_validate_prices)
    check("annualized_stats: rank-deficient 80-asset case", test_annualized_stats_rank_deficient)
    check("screen_universe: 200 -> 32", test_screen_universe)
    check("classical chunk solver: exact & greedy", test_classical_chunk_select_exact_and_greedy)
    check("tournament: 500 stocks, classical chunks", test_tournament_500_stocks_classical)
    check("tournament: budget near pool (proportional)", test_tournament_budget_near_pool)
    check("tournament: single chunk & hold-all", test_tournament_single_chunk_and_hold_all)
    check("QAOA chunk solver (4 qubits, local Aer)", test_qaoa_chunk_select)
    check("optimize_portfolio: quantum tournament (40 stocks)", test_optimize_portfolio_quantum_tournament)
    check("timeout -> classical fallback", test_timeout_falls_back_to_classical)
    check("SLSQP & min-variance weights", test_weights)
    check("IBM engine: clean errors without token", test_ibm_engine_errors_cleanly_without_token)
    check("random portfolio cloud", test_random_portfolios)
    check("live yfinance fetch (optional)", test_live_yfinance_optional)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("\nALL SMOKE TESTS PASSED")
