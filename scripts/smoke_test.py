"""Headless smoke test for the Quantum Portfolio Optimizer.

Exercises the full pipeline without a browser and (almost) without network:
  * extract_close shape handling (MultiIndex / flat / Series yfinance outputs)
  * annualized statistics
  * QAOA quantum stock selection on the Aer simulator (synthetic data)
  * the timeout -> classical fallback path
  * SLSQP weighting and the minimum-variance last resort
  * the full optimize_portfolio orchestrator
  * (optional) a live yfinance download — skipped gracefully if offline

Run with:  python scripts/smoke_test.py
"""

import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402  (imports streamlit in "bare" mode; main() is __main__-guarded)

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
    """A small, deliberately asymmetric market with a PSD covariance matrix."""
    rng = np.random.default_rng(seed)
    mu = np.array([0.18, 0.12, 0.25, 0.08])[:n]
    a = rng.normal(size=(n, n))
    sigma = 0.05 * (a @ a.T) / n + 0.01 * np.eye(n)
    return mu, sigma


def test_extract_close_multiindex():
    idx = pd.date_range("2025-01-01", periods=5, freq="B")
    cols = pd.MultiIndex.from_product([["Close", "Open"], ["AAPL", "MSFT"]],
                                      names=["Price", "Ticker"])
    raw = pd.DataFrame(np.arange(20.0).reshape(5, 4), index=idx, columns=cols)
    close = app.extract_close(raw, ["AAPL", "MSFT"])
    assert list(close.columns) == ["AAPL", "MSFT"], close.columns
    assert close.shape == (5, 2)


def test_extract_close_flat_and_series():
    idx = pd.date_range("2025-01-01", periods=5, freq="B")
    flat = pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 100},
        index=idx,
    )
    close = app.extract_close(flat, ["AAPL"])
    assert list(close.columns) == ["AAPL"] and close.shape == (5, 1)
    series = pd.Series(np.linspace(1, 2, 5), index=idx)
    close2 = app.extract_close(series, ["MSFT"])
    assert list(close2.columns) == ["MSFT"] and close2.shape == (5, 1)


def test_validate_prices():
    periods = app.MIN_HISTORY_DAYS + 20
    idx = pd.date_range("2025-01-01", periods=periods, freq="B")
    close = pd.DataFrame(
        {
            "AAPL": np.linspace(100, 110, periods),
            "BAD": np.nan,
            "MSFT": np.linspace(50, 55, periods),
            # a "recent IPO": only 20 non-NaN rows at the end
            "NEWIPO": [np.nan] * app.MIN_HISTORY_DAYS + list(np.linspace(10, 11, 20)),
        },
        index=idx,
    )
    aligned, valid, invalid, short = app.validate_prices(
        close, ["AAPL", "BAD", "MSFT", "NEWIPO"]
    )
    assert valid == ["AAPL", "MSFT"] and invalid == ["BAD"] and short == ["NEWIPO"]
    # the short ticker must NOT truncate the survivors' overlap window
    assert aligned.shape == (periods, 2)


def test_extract_close_rejects_unknown_shape():
    bad = pd.DataFrame({"Foo": [1.0, 2.0], "Bar": [3.0, 4.0]})
    try:
        app.extract_close(bad, ["AAPL"])
    except ValueError:
        return
    raise AssertionError("expected ValueError for an unrecognized data shape")


def test_annualized_stats():
    rng = np.random.default_rng(3)
    idx = pd.date_range("2025-01-01", periods=200, freq="B")
    prices = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, size=(200, 3)), axis=0)),
        index=idx, columns=["A", "B", "C"],
    )
    mu, sigma = app.annualized_stats(prices)
    assert mu.shape == (3,) and sigma.shape == (3, 3)
    assert np.linalg.eigvalsh(sigma).min() >= 0, "covariance must be PSD"


def test_qaoa_selection():
    mu, sigma = synthetic_market()
    selection = app.run_qaoa_selection(
        mu, sigma, q=1.0, budget=2, reps=1, shots=256, maxiter=40, seed=7
    )
    assert selection.sum() == 2, selection
    assert set(np.unique(selection)) <= {0, 1}


def test_exact_selection_matches_budget():
    mu, sigma = synthetic_market()
    selection = app.run_exact_selection(mu, sigma, q=1.0, budget=2)
    assert selection.sum() == 2, selection


def test_fallback_on_timeout():
    mu, sigma = synthetic_market()
    selection, method, reason = app.selection_with_fallback(
        mu, sigma, 1.0, 2,
        timeout_s=0.001,  # force the timeout path
        reps=1, shots=256, maxiter=30, seed=7,
    )
    assert method == "classical", method
    assert reason is not None and "time budget" in reason, reason
    assert selection.sum() == 2


def test_slsqp_weights():
    mu, sigma = synthetic_market()
    w = app.slsqp_weights(mu, sigma, q=1.0)
    assert abs(w.sum() - 1.0) < 1e-6 and (w >= -1e-9).all()
    equal = np.full(len(mu), 1.0 / len(mu))
    utility = lambda v: v @ mu - 1.0 * (v @ sigma @ v)  # noqa: E731
    assert utility(w) >= utility(equal) - 1e-9, "optimizer should beat equal weights"


def test_min_variance_weights():
    _, sigma = synthetic_market()
    w = app.min_variance_weights(sigma)
    assert abs(w.sum() - 1.0) < 1e-6 and (w >= -1e-9).all()


def test_optimize_portfolio_end_to_end():
    mu, sigma = synthetic_market()
    res = app.optimize_portfolio(
        mu, sigma, q=1.0, budget=2,
        reps=1, shots=256, maxiter=40, timeout_s=120, seed=7,
    )
    assert res["method"] == "quantum", res["method"]
    assert abs(res["weights"].sum() - 1.0) < 1e-6
    assert (res["weights"] > 0).sum() <= 2
    assert res["volatility"] > 0 and np.isfinite(res["exp_return"])


def test_random_portfolios():
    mu, sigma = synthetic_market()
    # __wrapped__ bypasses the st.cache_data wrapper outside a Streamlit runtime
    fn = getattr(app.random_portfolios, "__wrapped__", app.random_portfolios)
    rets, vols = fn(mu, sigma, 500, 42)
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
    close = app.extract_close(raw, ["AAPL"])
    assert "AAPL" in close.columns and len(close) > 0
    print(f"      live fetch OK: {len(close)} rows of AAPL closes")


if __name__ == "__main__":
    check("extract_close: MultiIndex columns", test_extract_close_multiindex)
    check("extract_close: flat frame & Series", test_extract_close_flat_and_series)
    check("extract_close: rejects unknown shapes", test_extract_close_rejects_unknown_shape)
    check("validate_prices: invalid & short-history tickers", test_validate_prices)
    check("annualized_stats", test_annualized_stats)
    check("QAOA quantum selection (4 qubits)", test_qaoa_selection)
    check("exact classical selection", test_exact_selection_matches_budget)
    check("timeout -> classical fallback", test_fallback_on_timeout)
    check("SLSQP stage-2 weights", test_slsqp_weights)
    check("minimum-variance last resort", test_min_variance_weights)
    check("optimize_portfolio end-to-end", test_optimize_portfolio_end_to_end)
    check("random portfolio cloud", test_random_portfolios)
    check("live yfinance fetch (optional)", test_live_yfinance_optional)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("\nALL SMOKE TESTS PASSED")
