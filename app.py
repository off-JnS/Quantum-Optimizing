"""
⚛️ Quantum Portfolio Optimizer
==============================

A single-file Streamlit app that:
  1. fetches one year of real daily price data for user-supplied stock tickers
     (Yahoo Finance via yfinance),
  2. runs a QAOA quantum optimization (qiskit-finance's PortfolioOptimization
     QUBO, solved on the local Qiskit Aer simulator — no IBM account needed)
     to select which stocks to hold,
  3. sizes the positions within the selected stocks with a classical optimizer
     using the same return-vs-risk objective, and
  4. displays the optimal allocation (pie chart, table, efficient frontier,
     metrics) with a plain-language explanation.

If the quantum step fails or exceeds its time budget, the app falls back to a
classical solver and clearly labels the result as a classical fallback.

Run with:  streamlit run app.py
"""

from __future__ import annotations

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

# --- Quantum stack -----------------------------------------------------------
# qiskit-optimization >= 0.7 vendors QAOA / COBYLA / NumPyMinimumEigensolver,
# so the whole pipeline needs no IBM account: everything runs on qiskit-aer.
from qiskit.transpiler import generate_preset_pass_manager
from qiskit_aer import AerSimulator
from qiskit_aer.primitives import SamplerV2 as AerSamplerV2
from qiskit_finance.applications.optimization import PortfolioOptimization
from qiskit_optimization.algorithms import MinimumEigenOptimizer
from qiskit_optimization.minimum_eigensolvers import QAOA, NumPyMinimumEigensolver
from qiskit_optimization.optimizers import COBYLA
from qiskit_optimization.utils import algorithm_globals

# ==============================================================================
# Constants
# ==============================================================================

MAX_TICKERS = 8            # one qubit per ticker — 8 keeps QAOA fast on a laptop simulator
MIN_HISTORY_DAYS = 60      # minimum overlapping trading days required
TRADING_DAYS = 252         # annualization factor for daily returns
N_RANDOM_PORTFOLIOS = 3000 # cloud of random portfolios for the efficient-frontier chart
QAOA_TIMEOUT_S = 90        # default time budget before falling back to a classical solver
DUST_WEIGHT = 0.0005       # hide sub-0.05% "dust" positions in the pie chart
SEED = 42

# Risk tolerance -> risk-aversion coefficient q in the objective  max  μ·w − q·(w·Σ·w).
# A cautious investor penalizes variance heavily; an aggressive one barely at all.
# NOTE: these values are calibrated for ANNUALIZED μ and Σ (daily stats × TRADING_DAYS);
# if you change the annualization, rescale q accordingly.
RISK_MAP = {"Low": 2.0, "Medium": 1.0, "High": 0.25}


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
) -> np.ndarray:
    """Stage 1 (quantum): QAOA on the local Aer simulator picks which stocks to hold."""
    algorithm_globals.random_seed = seed
    qp = _portfolio_qp(mu, sigma, q, budget)
    # The QAOA ansatz must be transpiled to the simulator's basis gates (V2 primitives).
    pass_manager = generate_preset_pass_manager(
        optimization_level=1, backend=AerSimulator()
    )
    qaoa = QAOA(
        sampler=AerSamplerV2(default_shots=shots, seed=seed),
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
    future = executor.submit(run_qaoa_selection, mu, sigma, q, budget, **qaoa_kwargs)
    try:
        return future.result(timeout=timeout_s), "quantum", None
    except FuturesTimeout:
        reason = f"QAOA did not finish within its {timeout_s:.0f} s time budget on the simulator"
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
    """Full two-stage pipeline: quantum stock selection, then classical sizing."""
    t0 = time.perf_counter()
    selection, method, reason = selection_with_fallback(
        mu, sigma, q, budget,
        timeout_s=timeout_s, reps=reps, shots=shots, maxiter=maxiter, seed=seed,
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
    mu: np.ndarray, sigma: np.ndarray, opt_ret: float, opt_vol: float, seed: int
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
            text=["Your portfolio"],
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
# UI
# ==============================================================================

def run_pipeline(
    tickers: list[str],
    amount: float,
    risk_label: str,
    budget: int | None,
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
        st.error(
            f"Please enter at most **{MAX_TICKERS}** tickers — each stock uses one "
            "qubit, and more would be slow on a local quantum simulator."
        )
        return

    try:
        with st.spinner(f"Fetching 1 year of daily prices for {', '.join(tickers)}…"):
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
    # The budget slider tracked the typed ticker list; if tickers were dropped it
    # can now be out of range — clamp it and tell the user instead of failing.
    if budget is None or not (1 <= budget <= n):
        clamped = min(n, max(2, n // 2))
        if budget is not None:
            st.info(
                f"Adjusted the number of stocks to hold from {budget} to {clamped} "
                f"because only {n} of your tickers have usable data."
            )
        budget = clamped

    try:
        with st.spinner(
            f"Running QAOA on the local Aer quantum simulator "
            f"({n} qubits, {2 ** n} possible stock combinations)…"
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

    # --- Method badge ---------------------------------------------------------
    if res["method"] == "quantum":
        st.success(
            f"✅ **Optimized with quantum QAOA** on the local Aer simulator "
            f"in {res['elapsed_s']:.1f} s."
        )
    elif res["method"] == "classical":
        st.warning(
            f"⚠️ **Classical fallback** — {res['fallback_reason']}. The same "
            "stock-selection problem was solved exactly by a classical algorithm instead."
        )
    else:
        st.warning(
            f"⚠️ **Classical fallback (minimum variance)** — {res['fallback_reason']}. "
            "A classical minimum-variance portfolio across all your stocks is shown instead."
        )

    # --- Headline metrics ------------------------------------------------------
    m1, m2, m3 = st.columns(3)
    m1.metric("Expected annual return", f"{res['exp_return'] * 100:.1f} %")
    m2.metric("Risk (annual volatility)", f"{res['volatility'] * 100:.1f} %")
    m3.metric("Sharpe ratio", f"{res['sharpe']:.2f}")

    # --- Allocation: pie + table ------------------------------------------------
    weights = res["weights"]
    amount = res["amount"]
    mu = res["mu"]
    table = (
        pd.DataFrame(
            {
                "Ticker": res["tickers"],
                "Allocation": weights * 100,
                "Amount": weights * amount,
                "Expected return contribution": weights * mu * 100,
                "Expected annual gain": weights * amount * mu,
            }
        )
        .sort_values("Allocation", ascending=False)
        .reset_index(drop=True)
    )

    col_pie, col_table = st.columns([1, 1.3])
    with col_pie:
        held = [
            (t, w * amount)
            for t, w in zip(res["tickers"], weights)
            if w > DUST_WEIGHT
        ]
        st.plotly_chart(
            pie_fig([t for t, _ in held], [a for _, a in held]),
            width="stretch",
        )
    with col_table:
        st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            column_config={
                "Ticker": st.column_config.TextColumn("Ticker"),
                "Allocation": st.column_config.NumberColumn(
                    "Allocation %", format="%.1f %%"
                ),
                "Amount": st.column_config.NumberColumn(
                    "Amount to invest", format="€ %.2f"
                ),
                "Expected return contribution": st.column_config.NumberColumn(
                    "Return contribution",
                    format="%.2f pp",
                    help="Percentage points of the portfolio's expected annual "
                         "return contributed by this stock (weight × its expected return).",
                ),
                "Expected annual gain": st.column_config.NumberColumn(
                    "Expected gain / year", format="€ %.2f"
                ),
            },
        )

    # --- Efficient frontier ------------------------------------------------------
    st.subheader("Efficient frontier")
    st.caption(
        f"{N_RANDOM_PORTFOLIOS:,} random portfolios built from your stocks. "
        "Up and to the left is better; the ⭐ marks your optimized portfolio."
    )
    st.plotly_chart(
        frontier_fig(
            res["mu"], res["sigma"], res["exp_return"], res["volatility"], res["seed"]
        ),
        width="stretch",
    )

    # --- Plain-language explanation ----------------------------------------------
    n = len(res["tickers"])
    # Use the solver's actual selection (not post-SLSQP weights) so the count
    # always matches the budget the text mentions.
    chosen = [t for t, s in zip(res["tickers"], res["selection"]) if s]
    if res["method"] == "quantum":
        step2 = (
            f"**2.** A quantum algorithm called **QAOA** — simulated locally on your "
            f"computer — used quantum superposition and interference to search among "
            f"all **{2 ** n}** possible combinations of your stocks for the best "
            f"balance of expected gain versus risk at your **{res['risk_label'].lower()}** "
            f"risk setting. It decided that your money belongs in "
            f"**{', '.join(chosen)}** ({res['budget']} of your {n} stocks)."
        )
    elif res["method"] == "classical":
        step2 = (
            f"**2.** The quantum step (QAOA) could not finish, so a regular computer "
            f"solved the very same stock-picking problem exactly and chose "
            f"**{', '.join(chosen)}**. That is why this result is labeled a "
            f"*classical fallback* — the answer is valid, it just wasn't found by "
            f"the quantum algorithm this time."
        )
    else:
        step2 = (
            "**2.** Both the quantum and the exact classical stock-picker failed, so "
            "the app built the *safest possible mix* of all your stocks instead "
            "(the classical minimum-variance portfolio)."
        )
    st.info(
        f"""
ℹ️ **What actually happened here, in plain language**

**1.** The app downloaded one year of daily prices for your {n} stocks and measured
how much each one tends to earn, how much it wobbles (risk), and how the stocks
move together.

{step2}

**3.** A classical optimizer then split your **€{amount:,.2f}** among the chosen
stocks using the same gain-versus-risk trade-off, giving the percentages above.

Quantum computers don't make stocks more profitable — what they promise is speed:
this kind of "pick the best combination" problem doubles in size with every stock
you add, and that is exactly where quantum algorithms like QAOA are expected to
shine as hardware matures. With {n} stocks a laptop can check every combination,
so today this app is an honest, working demonstration of the technique.
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
    st.caption(
        "Pick stocks with a quantum algorithm (QAOA) running on a local simulator — "
        "no IBM account, no API keys."
    )

    # --- Inputs -----------------------------------------------------------------
    tickers_text = st.text_input(
        "Stock tickers (comma-separated)",
        value="AAPL, MSFT, NVDA, SAP.DE",
        help=f"Yahoo Finance symbols, 2–{MAX_TICKERS} of them. "
             "Non-US stocks need their exchange suffix, e.g. SAP.DE or AIR.PA.",
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
            help="Low = prefer steadier stocks even if they earn less. "
                 "High = chase return, accept bigger swings.",
        )

    tickers = parse_tickers(tickers_text)
    n = len(tickers)

    # --- Advanced settings ---------------------------------------------------------
    with st.expander("⚙️ Advanced settings"):
        if 2 <= n <= MAX_TICKERS:
            budget = st.slider(
                "Number of stocks to hold (quantum selection budget)",
                min_value=1,
                max_value=n,
                value=min(n, max(2, n // 2)),
                key=f"budget_{n}",  # re-keyed so the range follows the ticker list
                help="QAOA picks exactly this many of your tickers to invest in.",
            )
        else:
            budget = None
        c1, c2 = st.columns(2)
        with c1:
            reps = st.slider("QAOA circuit depth (reps)", 1, 4, 2)
            shots = st.select_slider(
                "Simulator shots", options=[256, 512, 1024, 2048, 4096], value=1024
            )
            seed = int(st.number_input("Random seed", min_value=0, value=SEED, step=1))
        with c2:
            maxiter = st.slider("Classical optimizer iterations (COBYLA)", 25, 300, 150, step=25)
            timeout_s = st.slider(
                "QAOA time budget before classical fallback (seconds)",
                10, 300, QAOA_TIMEOUT_S, step=10,
            )

    # --- Action ----------------------------------------------------------------
    if st.button("🚀 Optimize", type="primary", width="stretch"):
        run_pipeline(
            tickers, amount, risk_label, budget,
            reps, shots, maxiter, timeout_s, seed,
        )

    # Results live in session_state so they survive Streamlit's reruns
    # (slider wiggles, window resizes, …) until the next optimization.
    if "result" in st.session_state:
        render_results(st.session_state["result"])


if __name__ == "__main__":
    main()
