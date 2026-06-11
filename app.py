"""
⚛️ Quantum Portfolio Optimizer — v2
=====================================

Improvements over v1:
  • Handles up to 500 stock tickers via hierarchical K-Means clustering.
    Stocks are grouped into up to QUANTUM_MAX_STOCKS clusters; QAOA selects
    which clusters to hold, then SLSQP sizes individual positions within them.
  • Optional IBM Quantum hardware backend — set IBM_QUANTUM_TOKEN (and optionally
    IBM_QUANTUM_BACKEND) in a .env file or environment variable to run QAOA on a
    real IBM quantum computer instead of the local Aer simulator.
  • Production-ready: all secrets read from the environment (Hostinger VPS / Docker).

Run with:  streamlit run app.py
"""

from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from scipy.optimize import minimize
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

# Load .env file automatically when python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Quantum stack -----------------------------------------------------------
from qiskit.transpiler import generate_preset_pass_manager
from qiskit_aer import AerSimulator
from qiskit_aer.primitives import SamplerV2 as AerSamplerV2
from qiskit_finance.applications.optimization import PortfolioOptimization
from qiskit_optimization.algorithms import MinimumEigenOptimizer
from qiskit_optimization.minimum_eigensolvers import QAOA, NumPyMinimumEigensolver
from qiskit_optimization.optimizers import COBYLA
from qiskit_optimization.utils import algorithm_globals

# IBM Quantum support (optional — only needed when IBM_QUANTUM_TOKEN is set)
_IBM_RUNTIME_AVAILABLE = False
try:
    from qiskit_ibm_runtime import QiskitRuntimeService
    from qiskit_ibm_runtime import SamplerV2 as IBMSamplerV2
    _IBM_RUNTIME_AVAILABLE = True
except ImportError:
    pass

# ==============================================================================
# Configuration
# ==============================================================================

MAX_TICKERS = 500           # maximum tickers the data layer will accept
QUANTUM_MAX_STOCKS = 20     # QAOA qubit limit; n > this triggers hierarchical clustering
MIN_HISTORY_DAYS = 60
TRADING_DAYS = 252
N_RANDOM_PORTFOLIOS = 3_000
QAOA_TIMEOUT_S = 120
DUST_WEIGHT = 0.0005
SEED = 42

RISK_MAP = {"Low": 2.0, "Medium": 1.0, "High": 0.25}

# IBM Quantum credentials — read from .env or shell environment
_IBM_TOKEN = os.environ.get("IBM_QUANTUM_TOKEN", "").strip()
_IBM_BACKEND_NAME = os.environ.get("IBM_QUANTUM_BACKEND", "").strip()


# ==============================================================================
# Quantum backend factory
# ==============================================================================

def _make_backend(
    n_qubits: int, shots: int, seed: int
) -> tuple:
    """Return (sampler, pass_manager, label, warning_or_None) for the active backend.

    When IBM_QUANTUM_TOKEN is set and qiskit-ibm-runtime is installed, this
    connects to IBM Quantum and picks the least-busy real device with at least
    n_qubits qubits (or the backend named by IBM_QUANTUM_BACKEND).
    Falls back to a local Aer simulator on any connection error.

    Safe to call outside a Streamlit context — warnings are returned as a
    plain string rather than emitted directly.
    """
    if _IBM_TOKEN and _IBM_RUNTIME_AVAILABLE:
        try:
            service = QiskitRuntimeService(channel="ibm_quantum", token=_IBM_TOKEN)
            if _IBM_BACKEND_NAME:
                backend = service.backend(_IBM_BACKEND_NAME)
            else:
                backend = service.least_busy(
                    operational=True, simulator=False, min_num_qubits=n_qubits
                )
            pm = generate_preset_pass_manager(optimization_level=1, backend=backend)
            sampler = IBMSamplerV2(backend)
            return sampler, pm, f"IBM Quantum ({backend.name})", None
        except Exception as exc:
            warn = (
                f"Could not connect to IBM Quantum ({exc}); "
                "falling back to local Aer simulator."
            )
            # fall through to Aer
    else:
        warn = None

    aer = AerSimulator()
    pm = generate_preset_pass_manager(optimization_level=1, backend=aer)
    sampler = AerSamplerV2(default_shots=shots, seed=seed)
    label = "Aer simulator (local)" if warn is None else "Aer simulator (IBM fallback)"
    return sampler, pm, label, warn


# ==============================================================================
# Data layer
# ==============================================================================

def parse_tickers(text: str) -> list[str]:
    """Split a comma-separated string into a deduplicated, uppercased ticker list."""
    seen: set[str] = set()
    tickers: list[str] = []
    for part in text.replace(";", ",").split(","):
        ticker = part.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


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


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_prices(tickers: tuple[str, ...]) -> pd.DataFrame:
    """Download 1 year of daily (adjusted) closes. Cached for an hour so
    re-running the optimizer on the same tickers does not re-download."""
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
    # Numerical guard: nudge Σ onto the positive-semidefinite cone if needed.
    if np.linalg.eigvalsh(sigma).min() < 1e-10:
        sigma = sigma + 1e-8 * np.eye(len(mu))
    return mu, sigma


# ==============================================================================
# Clustering layer  (for n > QUANTUM_MAX_STOCKS)
# ==============================================================================

def cluster_stocks(
    mu: np.ndarray, sigma: np.ndarray, n_clusters: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Group n stocks into n_clusters via K-Means on (return, volatility) features.

    Returns
    -------
    labels        (n,) int — cluster index per stock (0 … n_clusters-1)
    cluster_mu    (n_clusters,) — equal-weighted mean return per cluster
    cluster_sigma (n_clusters, n_clusters) — covariance between clusters
    """
    vols = np.sqrt(np.diag(sigma))
    features = np.column_stack([mu, vols])
    scaler = StandardScaler()
    features_scaled = scaler.fit_transform(features)

    kmeans = KMeans(n_clusters=n_clusters, random_state=SEED, n_init=10)
    labels = kmeans.fit_predict(features_scaled).astype(int)

    cluster_mu = np.zeros(n_clusters)
    cluster_sigma_arr = np.zeros((n_clusters, n_clusters))

    for i in range(n_clusters):
        idx_i = np.flatnonzero(labels == i)
        w_i = np.ones(len(idx_i)) / len(idx_i)
        cluster_mu[i] = float(w_i @ mu[idx_i])
        for j in range(n_clusters):
            idx_j = np.flatnonzero(labels == j)
            w_j = np.ones(len(idx_j)) / len(idx_j)
            block = sigma[np.ix_(idx_i, idx_j)]
            cluster_sigma_arr[i, j] = float(w_i @ block @ w_j)

    if np.linalg.eigvalsh(cluster_sigma_arr).min() < 1e-10:
        cluster_sigma_arr += 1e-8 * np.eye(n_clusters)

    return labels, cluster_mu, cluster_sigma_arr


# ==============================================================================
# Optimization layer
# ==============================================================================

def _portfolio_qp(mu: np.ndarray, sigma: np.ndarray, q: float, budget: int):
    """Build qiskit-finance's portfolio QUBO:
    maximize  μ·x − q·(x·Σ·x)   subject to   Σx = budget,   x ∈ {0,1}ⁿ."""
    problem = PortfolioOptimization(
        expected_returns=mu, covariances=sigma, risk_factor=q, budget=budget
    )
    return problem.to_quadratic_program()


def _check_feasible(result, budget: int) -> np.ndarray:
    selection = np.asarray(np.round(result.x), dtype=int)
    if result.status.name != "SUCCESS" or int(selection.sum()) != int(budget):
        raise RuntimeError(
            f"solver returned an infeasible selection "
            f"(picked {int(selection.sum())} stocks instead of {budget})"
        )
    return selection


def run_qaoa_selection(
    mu: np.ndarray,
    sigma: np.ndarray,
    q: float,
    budget: int,
    *,
    reps: int = 2,
    shots: int = 1024,
    maxiter: int = 150,
    seed: int = SEED,
    sampler=None,
    pass_manager=None,
) -> np.ndarray:
    """Stage 1 (quantum): QAOA picks which stocks/clusters to hold.

    If sampler and pass_manager are provided they are used as-is (IBM Quantum or
    a pre-built Aer instance); otherwise fresh Aer objects are created here.
    """
    algorithm_globals.random_seed = seed
    qp = _portfolio_qp(mu, sigma, q, budget)
    if sampler is None or pass_manager is None:
        aer = AerSimulator()
        pass_manager = generate_preset_pass_manager(optimization_level=1, backend=aer)
        sampler = AerSamplerV2(default_shots=shots, seed=seed)
    qaoa = QAOA(
        sampler=sampler,
        optimizer=COBYLA(maxiter=maxiter),
        reps=reps,
        pass_manager=pass_manager,
    )
    result = MinimumEigenOptimizer(qaoa).solve(qp)
    selection = np.asarray(np.round(result.x), dtype=int)
    if result.status.name == "SUCCESS" and int(selection.sum()) == int(budget):
        return selection
    # Shot noise can make the single lowest-energy sample violate the budget
    # constraint even when plenty of feasible samples were measured — rescue
    # the best feasible one before declaring the quantum run a failure.
    samples = sorted(getattr(result, "samples", None) or [], key=lambda s: s.fval)
    for sample in samples:
        x = np.asarray(np.round(sample.x), dtype=int)
        if int(x.sum()) == int(budget):
            return x
    raise RuntimeError(
        f"QAOA produced no measurement holding exactly {budget} stocks"
    )


def run_exact_selection(
    mu: np.ndarray, sigma: np.ndarray, q: float, budget: int
) -> np.ndarray:
    """Classical fallback: solve the exact same QUBO exactly (trivial for ≤ 8 stocks)."""
    qp = _portfolio_qp(mu, sigma, q, budget)
    result = MinimumEigenOptimizer(NumPyMinimumEigensolver()).solve(qp)
    return _check_feasible(result, budget)


def selection_with_fallback(
    mu: np.ndarray,
    sigma: np.ndarray,
    q: float,
    budget: int,
    *,
    timeout_s: float = QAOA_TIMEOUT_S,
    sampler=None,
    pass_manager=None,
    **qaoa_kwargs,
) -> tuple[np.ndarray | None, str, str | None]:
    """Try QAOA with a hard time budget; fall back to classical solvers.

    Returns (selection, method, fallback_reason) where method is one of
    "quantum", "classical" (exact solve of the same problem) or
    "min_variance" (last resort — caller computes minimum-variance weights).
    """
    # Deliberately NOT a `with` block: ThreadPoolExecutor.__exit__ would join the
    # still-running QAOA worker and defeat the timeout. shutdown(wait=False)
    # abandons it instead; the bounded shots/maxiter guarantee it terminates.
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        run_qaoa_selection, mu, sigma, q, budget,
        sampler=sampler, pass_manager=pass_manager,
        **qaoa_kwargs,
    )
    try:
        return future.result(timeout=timeout_s), "quantum", None
    except FuturesTimeout:
        reason = f"QAOA did not finish within its {timeout_s:.0f} s time budget"
    except Exception as exc:
        reason = f"QAOA failed ({exc})"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    try:
        return run_exact_selection(mu, sigma, q, budget), "classical", reason
    except Exception as exc:
        reason = f"{reason}; the exact classical solver also failed ({exc})"
        return None, "min_variance", reason


def _long_only_solve(objective, n: int) -> np.ndarray:
    """Minimize `objective(w)` over long-only weights summing to 1 (SLSQP).
    Falls back to equal weights if the solver returns NaN/inf or degenerates
    (note: a plain `sum <= 0` check would NOT catch NaN — NaN compares False)."""
    result = minimize(
        objective,
        np.full(n, 1.0 / n),
        method="SLSQP",
        bounds=[(0.0, 1.0)] * n,
        constraints=[{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}],
    )
    weights = np.clip(result.x, 0.0, None)
    if not np.isfinite(weights).all() or weights.sum() <= 0:
        weights = np.full(n, 1.0 / n)
    return weights / weights.sum()


def slsqp_weights(mu_sel: np.ndarray, sigma_sel: np.ndarray, q: float) -> np.ndarray:
    """Stage 2 (classical): continuous long-only weights within the selected stocks,
    maximizing the same utility  μ·w − q·(w·Σ·w)  with weights summing to 1."""
    return _long_only_solve(
        lambda w: -(mu_sel @ w - q * (w @ sigma_sel @ w)), len(mu_sel)
    )


def min_variance_weights(sigma: np.ndarray) -> np.ndarray:
    """Last-resort classical fallback: long-only minimum-variance portfolio
    across ALL assets (ignores expected returns entirely)."""
    return _long_only_solve(lambda w: w @ sigma @ w, len(sigma))


def optimize_portfolio(
    mu: np.ndarray,
    sigma: np.ndarray,
    q: float,
    budget: int,
    *,
    reps: int,
    shots: int,
    maxiter: int,
    timeout_s: float,
    seed: int,
) -> dict:
    """Direct two-stage pipeline for small universes (n ≤ QUANTUM_MAX_STOCKS).
    Gets the quantum backend (IBM or Aer) from the environment automatically.
    """
    t0 = time.perf_counter()
    sampler, pm, backend_label, ibm_warn = _make_backend(len(mu), shots, seed)
    selection, method, reason = selection_with_fallback(
        mu, sigma, q, budget,
        timeout_s=timeout_s, reps=reps, shots=shots, maxiter=maxiter, seed=seed,
        sampler=sampler, pass_manager=pm,
    )
    n = len(mu)
    if method == "min_variance":  # both solvers failed — minimum-variance over everything
        selection = np.ones(n, dtype=int)
        weights = min_variance_weights(sigma)
    else:
        idx = np.flatnonzero(selection)
        weights = np.zeros(n)
        weights[idx] = slsqp_weights(mu[idx], sigma[np.ix_(idx, idx)], q)

    exp_return = float(weights @ mu)
    volatility = float(np.sqrt(max(weights @ sigma @ weights, 0.0)))
    return {
        "method": method,
        "fallback_reason": reason,
        "selection": selection,
        "weights": weights,
        "exp_return": exp_return,
        "volatility": volatility,
        "sharpe": exp_return / volatility if volatility > 0 else 0.0,
        "elapsed_s": time.perf_counter() - t0,
        "clustered": False,
        "backend_label": backend_label,
        "ibm_warn": ibm_warn,
    }


def hierarchical_optimize_portfolio(
    mu: np.ndarray,
    sigma: np.ndarray,
    q: float,
    n_clusters: int,
    cluster_budget: int,
    *,
    reps: int,
    shots: int,
    maxiter: int,
    timeout_s: float,
    seed: int,
) -> dict:
    """Three-stage pipeline for large universes (n > QUANTUM_MAX_STOCKS).

    1. K-Means clusters all stocks into n_clusters groups.
    2. QAOA (n_clusters qubits) selects cluster_budget clusters to hold.
    3. SLSQP sizes individual positions within each selected cluster.
    """
    t0 = time.perf_counter()
    n = len(mu)
    sampler, pm, backend_label, ibm_warn = _make_backend(n_clusters, shots, seed)
    labels, cluster_mu, cluster_sigma = cluster_stocks(mu, sigma, n_clusters)

    selection, method, reason = selection_with_fallback(
        cluster_mu, cluster_sigma, q, cluster_budget,
        timeout_s=timeout_s, reps=reps, shots=shots, maxiter=maxiter, seed=seed,
        sampler=sampler, pass_manager=pm,
    )

    selected_clusters = (
        np.flatnonzero(selection) if selection is not None else np.arange(n_clusters)
    )
    if method == "min_variance":
        cluster_weights = min_variance_weights(cluster_sigma)
    else:
        c_mu_sel = cluster_mu[selected_clusters]
        c_sigma_sel = cluster_sigma[np.ix_(selected_clusters, selected_clusters)]
        cw_sel = slsqp_weights(c_mu_sel, c_sigma_sel, q)
        cluster_weights = np.zeros(n_clusters)
        cluster_weights[selected_clusters] = cw_sel

    final_weights = np.zeros(n)
    for c_idx in selected_clusters:
        if cluster_weights[c_idx] < 1e-9:
            continue
        stock_idx = np.flatnonzero(labels == c_idx)
        if len(stock_idx) == 1:
            final_weights[stock_idx[0]] = cluster_weights[c_idx]
        else:
            w_within = slsqp_weights(
                mu[stock_idx], sigma[np.ix_(stock_idx, stock_idx)], q
            )
            final_weights[stock_idx] = cluster_weights[c_idx] * w_within

    total = final_weights.sum()
    if total > 0:
        final_weights /= total

    # A stock is "selected" iff it belongs to a selected cluster
    stock_selection = np.zeros(n, dtype=int)
    for c_idx in selected_clusters:
        stock_selection[np.flatnonzero(labels == c_idx)] = 1

    exp_return = float(final_weights @ mu)
    volatility = float(np.sqrt(max(final_weights @ sigma @ final_weights, 0.0)))
    return {
        "method": method,
        "fallback_reason": reason,
        "selection": stock_selection,
        "weights": final_weights,
        "exp_return": exp_return,
        "volatility": volatility,
        "sharpe": exp_return / volatility if volatility > 0 else 0.0,
        "elapsed_s": time.perf_counter() - t0,
        "clustered": True,
        "n_clusters": n_clusters,
        "cluster_budget": cluster_budget,
        "labels": labels,
        "cluster_mu": cluster_mu,
        "cluster_sigma": cluster_sigma,
        "backend_label": backend_label,
        "ibm_warn": ibm_warn,
    }


# ==============================================================================
# Charts
# ==============================================================================

@st.cache_data(show_spinner=False)
def random_portfolios(
    mu: np.ndarray, sigma: np.ndarray, n_portfolios: int, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized cloud of random long-only portfolios for the frontier chart."""
    rng = np.random.default_rng(seed)
    w = rng.dirichlet(np.ones(len(mu)), n_portfolios)
    rets = w @ mu
    vols = np.sqrt(((w @ sigma) * w).sum(axis=1))
    return rets, vols


def pie_fig(tickers: list[str], amounts: list[float]) -> go.Figure:
    fig = go.Figure(
        go.Pie(
            labels=tickers,
            values=amounts,
            hole=0.35,
            textinfo="label+percent",
            hovertemplate="%{label}: €%{value:,.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=380,
        margin=dict(t=30, b=10, l=10, r=10),
        showlegend=False,
    )
    return fig


def frontier_fig(
    mu: np.ndarray, sigma: np.ndarray, opt_ret: float, opt_vol: float, seed: int,
    label: str = "Your portfolio",
) -> go.Figure:
    rets, vols = random_portfolios(mu, sigma, N_RANDOM_PORTFOLIOS, seed)
    sharpe = np.divide(rets, vols, out=np.zeros_like(rets), where=vols > 0)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=vols * 100,
            y=rets * 100,
            mode="markers",
            marker=dict(
                size=5,
                opacity=0.55,
                color=sharpe,
                colorscale="Viridis",
                showscale=True,
                colorbar=dict(title="Sharpe"),
            ),
            name="Random portfolios",
            hovertemplate="Risk %{x:.1f}% · Return %{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=[opt_vol * 100],
            y=[opt_ret * 100],
            mode="markers+text",
            marker=dict(symbol="star", size=22, color="#FFD166",
                        line=dict(width=1.5, color="white")),
            text=[label],
            textposition="top center",
            name="Optimized portfolio",
            hovertemplate="Risk %{x:.1f}% · Return %{y:.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        height=480,
        margin=dict(t=30, b=10, l=10, r=10),
        xaxis_title="Risk — annual volatility (%)",
        yaxis_title="Expected annual return (%)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return fig


# ==============================================================================
# UI helpers
# ==============================================================================

def run_pipeline(
    tickers: list[str],
    amount: float,
    risk_label: str,
    budget: int | None,
    n_clusters_override: int | None,
    reps: int,
    shots: int,
    maxiter: int,
    timeout_s: float,
    seed: int,
) -> None:
    """Validate inputs, fetch data, optimize, and store everything in session_state."""
    if len(tickers) < 2:
        st.error("Please enter at least **2** ticker symbols, separated by commas.")
        return
    if len(tickers) > MAX_TICKERS:
        st.error(f"Please enter at most **{MAX_TICKERS}** tickers.")
        return

    try:
        with st.spinner(f"Fetching 1 year of daily prices for {len(tickers)} tickers…"):
            # sorted() so "MSFT, AAPL" hits the same cache entry as "AAPL, MSFT";
            # validate_prices restores the user's column order afterwards.
            close_raw = fetch_prices(tuple(sorted(tickers)))
        close, valid, invalid, short = validate_prices(close_raw, tickers)
    except Exception as exc:
        st.error(
            "Could not download price data from Yahoo Finance. Please check your "
            "internet connection and ticker symbols, then try again. (Yahoo also "
            "rate-limits heavy usage — waiting a minute usually helps.)"
        )
        with st.expander("Technical details"):
            st.code(repr(exc))
        return

    if invalid:
        st.warning(
            f"No price data found for: **{', '.join(invalid)}** — these look like "
            "invalid tickers, so they were skipped. (Tip: non-US stocks need their "
            "exchange suffix, e.g. SAP.DE.)"
        )
    if short:
        st.warning(
            f"Excluded **{', '.join(short)}**: fewer than {MIN_HISTORY_DAYS} days of "
            "price history (e.g. a recent IPO or thinly traded listing) — too little "
            "data for meaningful statistics."
        )
    if len(valid) < 2:
        st.error("At least **2** tickers with valid price data are needed to optimize.")
        return
    if len(close) < MIN_HISTORY_DAYS:
        st.error(
            f"Only {len(close)} overlapping trading days of history were found — "
            f"at least {MIN_HISTORY_DAYS} are needed for meaningful statistics."
        )
        return

    mu, sigma = annualized_stats(close)
    q = RISK_MAP[risk_label]
    n = len(valid)
    use_clustering = n > QUANTUM_MAX_STOCKS

    if use_clustering:
        n_clusters = n_clusters_override or min(n, QUANTUM_MAX_STOCKS)
        cluster_budget = (
            budget if (budget is not None and 1 <= budget <= n_clusters)
            else max(1, n_clusters // 2)
        )
        backend_name = "IBM Quantum" if _IBM_TOKEN else "Aer simulator"
        try:
            with st.spinner(
                f"Clustering {n} stocks → {n_clusters} groups, then running "
                f"QAOA ({n_clusters} qubits) on {backend_name}…"
            ):
                result = hierarchical_optimize_portfolio(
                    mu, sigma, q, n_clusters, cluster_budget,
                    reps=reps, shots=shots, maxiter=maxiter,
                    timeout_s=timeout_s, seed=seed,
                )
        except Exception:
            st.error("The optimization failed unexpectedly. Please try again.")
            with st.expander("Technical details"):
                st.code(traceback.format_exc())
            return

        if result.get("ibm_warn"):
            st.warning(result["ibm_warn"])
        st.session_state["result"] = {
            **result,
            "tickers": valid,
            "invalid": invalid,
            "amount": float(amount),
            "risk_label": risk_label,
            "q": q,
            "mu": mu,
            "sigma": sigma,
            "seed": seed,
        }
        return

    # --- Direct mode (n ≤ QUANTUM_MAX_STOCKS) ------------------------------------
    if budget is None or not (1 <= budget <= n):
        clamped = min(n, max(2, n // 2))
        if budget is not None:
            st.info(
                f"Adjusted the number of stocks to hold from {budget} to {clamped} "
                f"because only {n} of your tickers have usable data."
            )
        budget = clamped

    backend_name = "IBM Quantum" if _IBM_TOKEN else "local Aer simulator"
    try:
        with st.spinner(
            f"Running QAOA ({n} qubits, {2 ** n} combinations) "
            f"on {backend_name}…"
        ):
            result = optimize_portfolio(
                mu, sigma, q, budget,
                reps=reps, shots=shots, maxiter=maxiter,
                timeout_s=timeout_s, seed=seed,
            )
    except Exception:
        st.error("The optimization failed unexpectedly. Please try again.")
        with st.expander("Technical details"):
            st.code(traceback.format_exc())
        return

    if result.get("ibm_warn"):
        st.warning(result["ibm_warn"])
    st.session_state["result"] = {
        **result,
        "tickers": valid,
        "invalid": invalid,
        "amount": float(amount),
        "risk_label": risk_label,
        "q": q,
        "budget": budget,
        "mu": mu,
        "sigma": sigma,
        "seed": seed,
    }


def render_results(res: dict) -> None:
    st.divider()

    # --- Backend / method badges -----------------------------------------------
    backend_label = res.get("backend_label", "")
    if "IBM Quantum" in backend_label:
        st.info(f"⚛️ **Quantum backend:** {backend_label}")
    elif backend_label:
        st.caption(f"⚛️ Backend: {backend_label}")

    if res["method"] == "quantum":
        mode = "hierarchical (clusters → stocks)" if res.get("clustered") else "direct"
        st.success(f"✅ **Quantum QAOA** ({mode}) finished in {res['elapsed_s']:.1f} s.")
    elif res["method"] == "classical":
        st.warning(
            f"⚠️ **Classical fallback** — {res['fallback_reason']}. "
            "The same problem was solved exactly by a classical algorithm."
        )
    else:
        st.warning(
            f"⚠️ **Classical fallback (minimum variance)** — {res['fallback_reason']}."
        )

    if res.get("clustered"):
        st.caption(
            f"📊 {len(res['tickers'])} stocks → {res['n_clusters']} clusters → "
            f"QAOA selected {res['cluster_budget']} cluster(s)."
        )

    # --- Headline metrics -------------------------------------------------------
    m1, m2, m3 = st.columns(3)
    m1.metric("Expected annual return", f"{res['exp_return'] * 100:.1f} %")
    m2.metric("Risk (annual volatility)", f"{res['volatility'] * 100:.1f} %")
    m3.metric("Sharpe ratio", f"{res['sharpe']:.2f}")

    # --- Allocation: pie + table ------------------------------------------------
    weights = res["weights"]
    amount = res["amount"]
    mu = res["mu"]

    table_data: dict = {
        "Ticker": res["tickers"],
        "Allocation %": weights * 100,
        "Amount (€)": weights * amount,
        "Return contribution (pp)": weights * mu * 100,
        "Est. annual gain (€)": weights * amount * mu,
    }
    if res.get("clustered"):
        table_data["Cluster"] = [int(lbl) for lbl in res["labels"]]

    table = (
        pd.DataFrame(table_data)
        .sort_values("Allocation %", ascending=False)
        .reset_index(drop=True)
    )

    col_pie, col_table = st.columns([1, 1.3])
    with col_pie:
        held = sorted(
            [(t, w * amount) for t, w in zip(res["tickers"], weights) if w > DUST_WEIGHT],
            key=lambda x: x[1], reverse=True,
        )
        # Limit to top-25 slices for readability on large portfolios
        shown = held[:25]
        if len(held) > 25:
            st.caption(f"Pie shows top-25 of {len(held)} positions.")
        st.plotly_chart(
            pie_fig([t for t, _ in shown], [a for _, a in shown]),
            width="stretch",
        )
    with col_table:
        col_cfg = {
            "Ticker": st.column_config.TextColumn("Ticker"),
            "Allocation %": st.column_config.NumberColumn("Allocation %", format="%.2f %%"),
            "Amount (€)": st.column_config.NumberColumn("Amount", format="€ %.2f"),
            "Return contribution (pp)": st.column_config.NumberColumn(
                "Return contribution",
                format="%.2f pp",
                help="Weight × expected annual return of this stock.",
            ),
            "Est. annual gain (€)": st.column_config.NumberColumn(
                "Est. annual gain", format="€ %.2f"
            ),
        }
        if res.get("clustered"):
            col_cfg["Cluster"] = st.column_config.NumberColumn("Cluster #")
        st.dataframe(table, hide_index=True, width="stretch", column_config=col_cfg)

    # --- Efficient frontier -----------------------------------------------------
    st.subheader("Efficient frontier")
    if res.get("clustered"):
        # Show the frontier at cluster level (fast, and the quantum stage operated here)
        c_mu = res["cluster_mu"]
        c_sigma = res["cluster_sigma"]
        c_labels = res["labels"]
        n_c = res["n_clusters"]
        c_weights = np.array(
            [weights[c_labels == c].sum() for c in range(n_c)]
        )
        opt_ret_f = float(c_weights @ c_mu)
        opt_vol_f = float(np.sqrt(max(c_weights @ c_sigma @ c_weights, 0.0)))
        st.caption(
            f"Frontier built from the {n_c} cluster representatives (the space "
            f"where QAOA operated). ⭐ = your optimized portfolio."
        )
        st.plotly_chart(
            frontier_fig(c_mu, c_sigma, opt_ret_f, opt_vol_f, res["seed"]),
            width="stretch",
        )
    else:
        st.caption(
            f"{N_RANDOM_PORTFOLIOS:,} random portfolios from your stocks. "
            "Up and to the left is better; ⭐ = your portfolio."
        )
        st.plotly_chart(
            frontier_fig(
                res["mu"], res["sigma"],
                res["exp_return"], res["volatility"], res["seed"]
            ),
            width="stretch",
        )

    # --- Plain-language explanation ---------------------------------------------
    n = len(res["tickers"])
    chosen = [t for t, s in zip(res["tickers"], res["selection"]) if s]
    n_chosen = len(chosen)

    if res.get("clustered"):
        n_clusters = res["n_clusters"]
        c_budget = res["cluster_budget"]
        if res["method"] == "quantum":
            step2 = (
                f"**2.** Because you supplied **{n} stocks** — more than the "
                f"{QUANTUM_MAX_STOCKS}-qubit limit — they were first grouped into "
                f"**{n_clusters} clusters** by return & risk similarity. QAOA then "
                f"explored all **2^{n_clusters} = {2**n_clusters:,}** cluster combinations "
                f"and selected the best **{c_budget}**, covering **{n_chosen} stocks**."
            )
        else:
            step2 = (
                f"**2.** Your {n} stocks were grouped into {n_clusters} clusters. "
                f"The quantum step fell back to a classical solver, which selected "
                f"{c_budget} cluster(s) covering {n_chosen} stocks."
            )
    elif res["method"] == "quantum":
        budget_shown = res.get("budget", n_chosen)
        step2 = (
            f"**2.** QAOA searched all **{2 ** n:,}** combinations of your stocks "
            f"for the best balance of gain vs. risk and chose "
            f"**{', '.join(chosen)}** ({budget_shown} of {n})."
        )
    elif res["method"] == "classical":
        step2 = (
            f"**2.** QAOA could not finish, so a classical solver chose "
            f"**{', '.join(chosen)}** exactly."
        )
    else:
        step2 = (
            "**2.** Both solvers failed; the app built a classical minimum-variance "
            "portfolio across all your stocks."
        )

    backend_desc = (
        f"IBM Quantum hardware ({backend_label})"
        if "IBM Quantum" in backend_label
        else "a local quantum simulator"
    )
    st.info(
        f"""
ℹ️ **What actually happened here, in plain language**

**1.** The app downloaded one year of daily prices for your {n} stocks and measured
how much each one tends to earn, how much it wobbles (risk), and how they move together.

{step2}

**3.** A classical SLSQP optimizer then split your **€{amount:,.2f}** among the chosen
stocks to maximize the same gain-vs-risk trade-off, giving the percentages above.

The quantum stage ran on **{backend_desc}**. Quantum computers don't make stocks more
profitable — what they promise is speed: this "pick the best combination" problem
grows exponentially with the number of inputs, and that is exactly where quantum
algorithms like QAOA are expected to outperform classical methods as hardware matures.
"""
    )
    st.caption(
        "Data: Yahoo Finance via yfinance · Educational demo — not financial advice."
    )


def main() -> None:
    st.set_page_config(
        page_title="Quantum Portfolio Optimizer",
        page_icon="⚛️",
        layout="wide",
    )
    st.title("⚛️ Quantum Portfolio Optimizer")

    # --- Backend status banner -------------------------------------------------
    if _IBM_TOKEN:
        if _IBM_RUNTIME_AVAILABLE:
            be = f" · backend: **{_IBM_BACKEND_NAME}**" if _IBM_BACKEND_NAME else " (least-busy auto-select)"
            st.success(
                f"🔌 **IBM Quantum** backend configured{be} — "
                "QAOA will run on real quantum hardware."
            )
        else:
            st.warning(
                "⚠️ IBM_QUANTUM_TOKEN is set but `qiskit-ibm-runtime` is not installed. "
                "Run `pip install qiskit-ibm-runtime` to enable real hardware."
            )
    else:
        st.caption(
            "Running on the **local Aer** quantum simulator. "
            "Set **IBM_QUANTUM_TOKEN** in your environment or `.env` file "
            "to use a real IBM quantum computer."
        )

    # --- Inputs ----------------------------------------------------------------
    tickers_text = st.text_area(
        "Stock tickers (comma-separated, up to 500)",
        value="AAPL, MSFT, NVDA, SAP.DE",
        height=80,
        help=(
            f"Yahoo Finance symbols — 2 to {MAX_TICKERS}. "
            "For large lists paste one per line or comma-separated. "
            "Non-US stocks need their exchange suffix, e.g. SAP.DE, AIR.PA, 7203.T."
        ),
    )
    col_amount, col_risk = st.columns(2)
    with col_amount:
        amount = st.number_input(
            "Total investment (€)", min_value=100.0, value=10_000.0, step=500.0
        )
    with col_risk:
        risk_label = st.select_slider(
            "Risk tolerance",
            options=list(RISK_MAP),
            value="Medium",
            help="Low = prefer steadier stocks. High = chase return, accept bigger swings.",
        )

    tickers = parse_tickers(tickers_text)
    n = len(tickers)
    use_clustering = n > QUANTUM_MAX_STOCKS

    # --- Advanced settings -----------------------------------------------------
    with st.expander("⚙️ Advanced settings"):
        if use_clustering:
            st.info(
                f"🧩 **Hierarchical mode** — {n} stocks exceed the "
                f"{QUANTUM_MAX_STOCKS}-qubit QAOA limit. "
                "Stocks will be clustered before the quantum stage."
            )
            n_clusters = st.slider(
                "Number of clusters (= QAOA qubits)",
                min_value=2,
                max_value=min(n, QUANTUM_MAX_STOCKS),
                value=min(n, QUANTUM_MAX_STOCKS),
                help=(
                    "Each cluster = one qubit. More clusters = finer resolution "
                    "but longer QAOA runtime."
                ),
            )
            budget = st.slider(
                "Number of clusters to hold (quantum budget)",
                min_value=1,
                max_value=n_clusters,
                value=max(1, n_clusters // 2),
                key=f"budget_cluster_{n_clusters}",
            )
        elif 2 <= n:
            n_clusters = None
            budget = st.slider(
                "Number of stocks to hold (quantum budget)",
                min_value=1,
                max_value=n,
                value=min(n, max(2, n // 2)),
                key=f"budget_{n}",
                help="QAOA picks exactly this many of your tickers.",
            )
        else:
            n_clusters = None
            budget = None

        c1, c2 = st.columns(2)
        with c1:
            reps = st.slider("QAOA circuit depth (reps)", 1, 4, 2)
            shots = st.select_slider(
                "Shots", options=[256, 512, 1024, 2048, 4096], value=1024
            )
            seed = int(st.number_input("Random seed", min_value=0, value=SEED, step=1))
        with c2:
            maxiter = st.slider("COBYLA iterations", 25, 300, 150, step=25)
            timeout_s = st.slider(
                "QAOA time budget before classical fallback (s)",
                10, 600, QAOA_TIMEOUT_S, step=10,
            )

        # Informational IBM panel (config is via env vars, not editable in the UI)
        if _IBM_TOKEN:
            with st.container(border=True):
                st.markdown("**IBM Quantum connection**")
                st.text_input(
                    "Token (IBM_QUANTUM_TOKEN)", value="●●●●●●●●", disabled=True
                )
                st.text_input(
                    "Backend (IBM_QUANTUM_BACKEND — empty = least busy)",
                    value=_IBM_BACKEND_NAME or "(auto-select least busy)",
                    disabled=True,
                )

    # --- Action ----------------------------------------------------------------
    if st.button("🚀 Optimize", type="primary", use_container_width=True):
        run_pipeline(
            tickers, amount, risk_label, budget,
            n_clusters if use_clustering else None,
            reps, shots, maxiter, timeout_s, seed,
        )

    # Results survive Streamlit reruns (slider wiggles, resizes) via session_state
    if "result" in st.session_state:
        render_results(st.session_state["result"])


if __name__ == "__main__":
    main()
