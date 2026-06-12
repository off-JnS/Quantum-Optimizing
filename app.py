"""
⚛️ Quantum Portfolio Optimizer — Streamlit UI
=============================================

Optimizes a stock portfolio of up to 500 tickers with QAOA running either on
real IBM Quantum hardware (cloud API) or on the local Qiskit Aer simulator.

Pipeline (see quantum_portfolio/optimizer.py for details):
  data download -> classical screening -> hierarchical QAOA tournament
  -> classical position sizing -> charts, tables and a plain-language report.

Run locally with:   streamlit run app.py
Deploy with Docker:  docker compose up -d   (see docs/DEPLOY_HOSTINGER.md)
"""

from __future__ import annotations

import os
import traceback

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# Load a local .env file (IBM_QUANTUM_TOKEN, IBM_QUANTUM_BACKEND, …) if present,
# so local runs and bare-metal servers behave like the Docker deployment.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover - pinned in requirements
    pass

from quantum_portfolio import MAX_TICKERS, MIN_HISTORY_DAYS
from quantum_portfolio.data import (
    annualized_stats,
    download_close,
    parse_tickers,
    validate_prices,
)
from quantum_portfolio.engines import (
    build_ibm_engine,
    build_local_engine,
    ibm_connection_report,
    resolve_ibm_instance,
    resolve_ibm_token,
)
from quantum_portfolio.optimizer import (
    RISK_MAP,
    SEED,
    optimize_portfolio,
    random_portfolios,
)

N_RANDOM_PORTFOLIOS = 3000  # cloud size for the efficient-frontier chart
DUST_WEIGHT = 0.0005        # hide sub-0.05% "dust" positions in the pie chart
MAX_PIE_SLICES = 15         # group smaller holdings into "Other" beyond this
DEFAULT_TICKERS = "AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, JPM, V, JNJ, XOM, SAP.DE"

MODE_IBM = "IBM Quantum hardware (cloud API)"
MODE_LOCAL = "Local simulator (this machine)"


# ==============================================================================
# Cached wrappers around the pure package functions
# ==============================================================================

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_prices(tickers: tuple[str, ...]) -> pd.DataFrame:
    """Cached for an hour so re-running the optimizer on the same universe
    doesn't re-download ~500 price histories."""
    return download_close(tickers)


@st.cache_data(show_spinner=False)
def cached_random_portfolios(mu: np.ndarray, sigma: np.ndarray, n: int, seed: int):
    return random_portfolios(mu, sigma, n, seed)


# ==============================================================================
# Charts
# ==============================================================================

def pie_fig(tickers: list[str], amounts: list[float]) -> go.Figure:
    """Donut of euro amounts; small holdings grouped into 'Other' so a
    30-stock portfolio stays readable."""
    if len(tickers) > MAX_PIE_SLICES:
        keep = MAX_PIE_SLICES - 1
        other = float(sum(amounts[keep:]))
        tickers = tickers[:keep] + [f"Other ({len(amounts) - keep} stocks)"]
        amounts = amounts[:keep] + [other]
    fig = go.Figure(
        go.Pie(
            labels=tickers,
            values=amounts,
            hole=0.35,
            textinfo="label+percent",
            hovertemplate="%{label}: €%{value:,.2f}<extra></extra>",
        )
    )
    fig.update_layout(height=400, margin=dict(t=30, b=10, l=10, r=10), showlegend=False)
    return fig


def frontier_fig(
    mu_pool: np.ndarray, sigma_pool: np.ndarray, opt_ret: float, opt_vol: float, seed: int
) -> go.Figure:
    rets, vols = cached_random_portfolios(mu_pool, sigma_pool, N_RANDOM_PORTFOLIOS, seed)
    sharpe = np.divide(rets, vols, out=np.zeros_like(rets), where=vols > 0)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=vols * 100,
            y=rets * 100,
            mode="markers",
            marker=dict(
                size=5, opacity=0.55, color=sharpe, colorscale="Viridis",
                showscale=True, colorbar=dict(title="Sharpe"),
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
# Sidebar — quantum engine & algorithm configuration
# ==============================================================================

def sidebar_config() -> dict:
    st.sidebar.header("⚙️ Quantum engine")
    mode = st.sidebar.radio(
        "Where should the quantum circuits run?",
        [MODE_IBM, MODE_LOCAL],
        help="IBM Quantum runs QAOA on a real quantum processor via the cloud "
             "API. The local simulator is free, instant and needs no account.",
    )
    is_ibm = mode == MODE_IBM

    token = instance = backend_name = ""
    allow_fallback = True
    if is_ibm:
        token = st.sidebar.text_input(
            "IBM Quantum API key",
            type="password",
            help="Create one at https://quantum.cloud.ibm.com → Access management "
                 "→ API keys. On a server, set the IBM_QUANTUM_TOKEN environment "
                 "variable instead of pasting it here.",
        )
        if not resolve_ibm_token(token):
            st.sidebar.info(
                "No API key in this field. The app will try a saved account "
                "(QiskitRuntimeService.save_account) or IBM_QUANTUM_TOKEN; if "
                "none exists, runs fall back to the local simulator."
            )
        instance = st.sidebar.text_input(
            "Instance CRN (recommended)",
            value=os.environ.get("IBM_QUANTUM_INSTANCE", ""),
            help="Copy it from the Instances page at quantum.cloud.ibm.com. "
                 "Leave empty only if your account has a single default "
                 "instance. Can be preset via IBM_QUANTUM_INSTANCE.",
        )
        backend_name = st.sidebar.text_input(
            "Backend name (optional)",
            value=os.environ.get("IBM_QUANTUM_BACKEND", ""),
            help="e.g. ibm_torino. Leave empty to automatically pick the least "
                 "busy device. Can also be preset via the IBM_QUANTUM_BACKEND "
                 "environment variable.",
        )
        # Verify credentials WITHOUT running a job (and so without spending quota).
        if st.sidebar.button("🔌 Test IBM connection"):
            try:
                with st.spinner("Connecting to IBM Quantum…"):
                    report = ibm_connection_report(
                        resolve_ibm_token(token), resolve_ibm_instance(instance)
                    )
                st.sidebar.success(
                    f"Connected — {len(report)} backend(s) available."
                )
                st.sidebar.dataframe(
                    report, hide_index=True,
                    column_config={
                        "name": "Backend", "qubits": "Qubits",
                        "pending_jobs": "Queue", "simulator": "Sim",
                    },
                )
            except Exception as exc:
                st.sidebar.error(str(exc))
        allow_fallback = st.sidebar.checkbox(
            "Fall back to the local simulator if IBM is unavailable", value=True,
            help="Uncheck this to force every calculation onto IBM hardware "
                 "(the run will error instead of computing locally).",
        )
        st.sidebar.caption(
            "💡 Hardware runs queue behind other users and consume your IBM "
            "quota (the free Open plan gives ~10 min of QPU time per 28 days). "
            "Each optimizer iteration is a separate job, so for an all-hardware "
            "run keep the **universe ≤ the chunk size** (one QAOA optimization) "
            "and the iterations low. Full setup: docs/IBM_SETUP.md."
        )

    with st.sidebar.expander("🔬 Algorithm settings"):
        pool_size = st.slider(
            "Quantum pool size (stocks screened in)", 8, 64, 32,
            help="A classical pre-screen ranks all your tickers by risk-adjusted "
                 "score and passes the best ones to the quantum stage.",
        )
        chunk_default, chunk_max = (16, 32) if is_ibm else (10, 16)
        chunk_size = st.slider(
            "Qubits per subproblem (chunk size)", 4, chunk_max, chunk_default,
            key=f"chunk_{is_ibm}",
            help="The tournament splits the pool into chunks of this many "
                 "stocks; each chunk is one QAOA run with one qubit per stock.",
        )
        reps = st.slider("QAOA circuit depth (reps)", 1, 4, 1 if is_ibm else 2,
                         key=f"reps_{is_ibm}")
        shots = st.select_slider(
            "Shots per circuit", options=[256, 512, 1024, 2048, 4096],
            value=2048 if is_ibm else 1024, key=f"shots_{is_ibm}",
        )
        maxiter = st.slider(
            "Optimizer iterations (COBYLA)", 5, 300, 20 if is_ibm else 150,
            step=5, key=f"maxiter_{is_ibm}",
            help="On real hardware every iteration is a separate cloud job — "
                 "keep this small to respect queue times and your quota.",
        )
        timeout_s = st.slider(
            "Time budget before classical fallback (seconds)",
            30, 7200, 3600 if is_ibm else 120, step=30, key=f"timeout_{is_ibm}",
        )
        seed = int(st.number_input("Random seed", min_value=0, value=SEED, step=1))

    st.sidebar.caption("📚 Full guide: see docs/USAGE.md in the repository.")
    return {
        "is_ibm": is_ibm,
        "token": token,
        "instance": instance,
        "backend_name": backend_name,
        "allow_fallback": allow_fallback,
        "pool_size": pool_size,
        "chunk_size": chunk_size,
        "reps": reps,
        "shots": shots,
        "maxiter": maxiter,
        "timeout_s": timeout_s,
        "seed": seed,
    }


def build_engine(cfg: dict):
    """Build the requested engine; optionally fall back to the local simulator
    with a visible warning (never silently)."""
    if not cfg["is_ibm"]:
        return build_local_engine(cfg["shots"], cfg["seed"])
    try:
        return build_ibm_engine(
            resolve_ibm_token(cfg["token"]),
            resolve_ibm_instance(cfg["instance"]),
            cfg["backend_name"],
            cfg["shots"],
            min_qubits=cfg["chunk_size"],
        )
    except ValueError as exc:
        if not cfg["allow_fallback"]:
            st.error(f"IBM Quantum is not reachable: {exc}")
            return None
        st.warning(f"⚠️ {exc} — **using the local simulator instead.**")
        return build_local_engine(cfg["shots"], cfg["seed"])


# ==============================================================================
# Pipeline
# ==============================================================================

def _shortlist(names: list[str], limit: int = 12) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" … and {len(names) - limit} more" if len(names) > limit else "")


def run_pipeline(tickers: list[str], amount: float, risk_label: str,
                 budget: int, cfg: dict) -> None:
    """Validate inputs, fetch data, optimize, and store everything in session_state."""
    if len(tickers) < 2:
        st.error("Please enter at least **2** ticker symbols.")
        return
    if len(tickers) > MAX_TICKERS:
        st.error(f"Please enter at most **{MAX_TICKERS}** tickers "
                 f"(you entered {len(tickers)}).")
        return

    try:
        with st.spinner(
            f"Downloading 1 year of daily prices for {len(tickers)} tickers — "
            "large universes can take a minute on the first run…"
        ):
            # sorted() so the cache is order-independent; validate_prices
            # restores the user's column order afterwards.
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
            f"No price data found for {len(invalid)} ticker(s): "
            f"**{_shortlist(invalid)}** — skipped. (Tip: non-US stocks need an "
            "exchange suffix, e.g. SAP.DE.)"
        )
    if short:
        st.warning(
            f"Excluded {len(short)} ticker(s) with fewer than {MIN_HISTORY_DAYS} "
            f"days of history: **{_shortlist(short)}**."
        )
    if len(valid) < 2:
        st.error("At least **2** tickers with valid price data are needed.")
        return
    if len(close) < MIN_HISTORY_DAYS:
        st.error(
            f"Only {len(close)} overlapping trading days were found across these "
            f"markets — at least {MIN_HISTORY_DAYS} are needed."
        )
        return

    mu, sigma = annualized_stats(close)
    n = len(valid)
    if budget > n:
        st.info(f"Adjusted the number of stocks to hold from {budget} to {n} "
                f"because only {n} tickers have usable data.")
        budget = n

    engine = build_engine(cfg)
    if engine is None:
        return

    try:
        with st.spinner(
            f"Optimizing on the {engine.label}: screening {n} stocks, then "
            f"running QAOA subproblems of up to {min(cfg['chunk_size'], engine.max_qubits)} "
            "qubits… (hardware jobs may queue — see the time budget in the sidebar)"
        ):
            result = optimize_portfolio(
                mu, sigma, RISK_MAP[risk_label], budget, engine,
                pool_size=cfg["pool_size"], chunk_size=cfg["chunk_size"],
                reps=cfg["reps"], maxiter=cfg["maxiter"],
                timeout_s=cfg["timeout_s"], seed=cfg["seed"],
            )
    except Exception:
        st.error("The optimization failed unexpectedly. Please try again.")
        with st.expander("Technical details"):
            st.code(traceback.format_exc())
        return

    st.session_state["result"] = {
        **result,
        "tickers": valid,
        "amount": float(amount),
        "risk_label": risk_label,
        "budget": budget,
        "mu": mu,
        "sigma": sigma,
        "seed": cfg["seed"],
    }


# ==============================================================================
# Results
# ==============================================================================

def render_results(res: dict) -> None:
    st.divider()
    stats = res["stats"]

    if res["method"] == "quantum":
        st.success(
            f"✅ **Optimized with QAOA on the {res['engine_label']}** in "
            f"{res['elapsed_s']:.1f} s — {stats['subproblems']} quantum "
            f"subproblem(s), up to {stats['max_chunk']} qubits each."
        )
    elif res["method"] == "classical":
        st.warning(
            f"⚠️ **Classical fallback** — {res['fallback_reason']}. The same "
            "selection tournament was solved by a classical algorithm instead."
        )
    else:
        st.warning(
            f"⚠️ **Classical fallback (minimum variance)** — {res['fallback_reason']}."
        )

    m1, m2, m3 = st.columns(3)
    m1.metric("Expected annual return", f"{res['exp_return'] * 100:.1f} %")
    m2.metric("Risk (annual volatility)", f"{res['volatility'] * 100:.1f} %")
    m3.metric("Sharpe ratio", f"{res['sharpe']:.2f}")

    weights = res["weights"]
    amount = res["amount"]
    mu = res["mu"]
    tickers = res["tickers"]

    full_table = pd.DataFrame(
        {
            "Ticker": tickers,
            "Allocation %": weights * 100,
            "Amount (EUR)": weights * amount,
            "Return contribution (pp)": weights * mu * 100,
            "Expected annual gain (EUR)": weights * amount * mu,
        }
    ).sort_values("Allocation %", ascending=False).reset_index(drop=True)
    # The table lists every stock the selection stage chose — even ones the
    # sizing stage then weighted to ~0% — so selection and table always agree.
    chosen_set = {t for t, s in zip(tickers, res["selection"]) if s}
    held_table = full_table[
        full_table["Ticker"].isin(chosen_set)
        | (full_table["Allocation %"] > DUST_WEIGHT * 100)
    ]
    pie_rows = full_table[full_table["Allocation %"] > DUST_WEIGHT * 100]

    tab_alloc, tab_frontier, tab_explain = st.tabs(
        ["📊 Allocation", "🌌 Efficient frontier", "🧠 What happened"]
    )

    with tab_alloc:
        col_pie, col_table = st.columns([1, 1.3])
        with col_pie:
            st.plotly_chart(
                pie_fig(pie_rows["Ticker"].tolist(),
                        pie_rows["Amount (EUR)"].tolist()),
                width="stretch",
            )
        with col_table:
            st.dataframe(
                held_table,
                hide_index=True,
                width="stretch",
                column_config={
                    "Ticker": st.column_config.TextColumn("Ticker"),
                    "Allocation %": st.column_config.NumberColumn(
                        "Allocation %", format="%.2f %%"
                    ),
                    "Amount (EUR)": st.column_config.NumberColumn(
                        "Amount to invest", format="€ %.2f"
                    ),
                    "Return contribution (pp)": st.column_config.NumberColumn(
                        "Return contribution", format="%.2f pp",
                        help="Percentage points of the portfolio's expected "
                             "annual return contributed by this stock.",
                    ),
                    "Expected annual gain (EUR)": st.column_config.NumberColumn(
                        "Expected gain / year", format="€ %.2f"
                    ),
                },
            )
        st.download_button(
            "⬇️ Download full allocation as CSV (all tickers)",
            full_table.to_csv(index=False).encode(),
            file_name="quantum_portfolio_allocation.csv",
            mime="text/csv",
        )

    with tab_frontier:
        pool_idx = res["pool_indices"]
        mu_pool = res["mu"][pool_idx]
        sigma_pool = res["sigma"][np.ix_(pool_idx, pool_idx)]
        st.caption(
            f"{N_RANDOM_PORTFOLIOS:,} random portfolios built from the "
            f"{len(pool_idx)} screened candidate stocks. Up and to the left is "
            "better; the ⭐ marks your optimized portfolio."
        )
        st.plotly_chart(
            frontier_fig(mu_pool, sigma_pool, res["exp_return"],
                         res["volatility"], res["seed"]),
            width="stretch",
        )

    with tab_explain:
        render_explanation(res)

    st.caption(
        "Data: Yahoo Finance via yfinance · Educational demo — not financial advice."
    )


def render_explanation(res: dict) -> None:
    n = len(res["tickers"])
    pool_n = len(res["pool_indices"])
    stats = res["stats"]
    chosen = [t for t, s in zip(res["tickers"], res["selection"]) if s]
    amount = res["amount"]

    steps = [
        f"**1. Measured the market.** Downloaded one year of daily prices for "
        f"your {n} stocks and measured how much each tends to earn, how much it "
        f"wobbles (risk), and how they move together."
    ]
    if res["screened"]:
        steps.append(
            f"**2. Screened the field.** A classical pre-screen ranked all {n} "
            f"stocks by gain-versus-risk at your **{res['risk_label'].lower()}** "
            f"risk setting and passed the best **{pool_n}** into the quantum round."
        )
    if res["method"] == "quantum":
        steps.append(
            f"**{len(steps) + 1}. Quantum tournament.** The QAOA quantum algorithm "
            f"ran **{stats['subproblems']} subproblem(s)** of up to "
            f"**{stats['max_chunk']} qubits** on the **{res['engine_label']}** — "
            f"each one searching every combination of its group of stocks at once "
            f"for the best gain-versus-risk balance. Winners advanced through "
            f"{max(stats['rounds'], 1)} round(s) until exactly "
            f"**{len(chosen)}** stocks remained: {_shortlist(chosen)}."
        )
    elif res["method"] == "classical":
        steps.append(
            f"**{len(steps) + 1}. Selection (classical fallback).** The quantum "
            f"run could not finish ({res['fallback_reason']}), so a regular "
            f"computer played the same tournament and chose {_shortlist(chosen)}. "
            "The answer is valid — it just wasn't found by the quantum algorithm "
            "this time."
        )
    else:
        steps.append(
            f"**{len(steps) + 1}. Selection failed entirely**, so the app built "
            "the safest possible mix of the screened stocks instead (the "
            "classical minimum-variance portfolio)."
        )
    steps.append(
        f"**{len(steps) + 1}. Sized the positions.** A classical optimizer split "
        f"your **€{amount:,.2f}** among the chosen stocks using the same "
        "gain-versus-risk trade-off, giving the percentages in the Allocation tab."
    )

    st.info(
        "ℹ️ **What actually happened here, in plain language**\n\n"
        + "\n\n".join(steps)
        + "\n\n---\n\nQuantum computers don't make stocks more profitable — "
        "their promise is speed. 'Pick the best combination' problems double in "
        "size with every stock you add, which is exactly where quantum "
        "algorithms like QAOA are expected to shine as hardware matures. The "
        "chunked tournament above is how today's limited qubit counts are "
        "stretched to a 500-stock universe."
    )


# ==============================================================================
# Main
# ==============================================================================

def main() -> None:
    st.set_page_config(
        page_title="Quantum Portfolio Optimizer",
        page_icon="⚛️",
        layout="wide",
    )
    st.title("⚛️ Quantum Portfolio Optimizer")
    st.caption(
        "Portfolio selection with QAOA on real IBM Quantum hardware (or a local "
        "simulator) — up to 500 stocks, hybrid quantum-classical pipeline."
    )

    cfg = sidebar_config()

    with st.expander("📋 How to use (quick guide)"):
        st.markdown(
            f"""
1. **Pick the engine** in the sidebar — IBM Quantum hardware (needs a free API
   key from [quantum.cloud.ibm.com](https://quantum.cloud.ibm.com)) or the
   local simulator.
2. **Enter tickers** below (comma, space or newline separated, 2–{MAX_TICKERS}).
   Paste a whole index if you like.
3. **Set the amount, risk tolerance and portfolio size**, then hit **Optimize**.
4. Read the results in the three tabs — including an honest explanation of
   what the quantum computer actually did.

Full manual: [`docs/USAGE.md`](https://github.com/off-JnS/Quantum-Optimizing/blob/HEAD/docs/USAGE.md) ·
Hosting guide: [`docs/DEPLOY_HOSTINGER.md`](https://github.com/off-JnS/Quantum-Optimizing/blob/HEAD/docs/DEPLOY_HOSTINGER.md)
"""
        )

    st.subheader("1️⃣ Stocks")
    tickers_text = st.text_area(
        "Ticker symbols (comma, space or newline separated)",
        value=DEFAULT_TICKERS,
        height=110,
        help=f"Yahoo Finance symbols, 2–{MAX_TICKERS}. Non-US stocks need their "
             "exchange suffix, e.g. SAP.DE or AIR.PA.",
    )
    tickers = parse_tickers(tickers_text)
    st.caption(f"**{len(tickers)}** tickers recognized (maximum {MAX_TICKERS}).")

    st.subheader("2️⃣ Investment")
    col_amount, col_risk, col_budget = st.columns(3)
    with col_amount:
        amount = st.number_input(
            "Total investment (€)", min_value=100.0, value=10_000.0, step=500.0
        )
    with col_risk:
        risk_label = st.select_slider(
            "Risk tolerance", options=list(RISK_MAP), value="Medium",
            help="Low = prefer steadier stocks even if they earn less. "
                 "High = chase return, accept bigger swings.",
        )
    with col_budget:
        budget_max = max(2, min(40, len(tickers))) if tickers else 2
        budget = st.slider(
            "Number of stocks to hold", 1, budget_max,
            min(10, max(2, budget_max // 2)),
            key=f"budget_{budget_max}",
            help="The quantum tournament selects exactly this many stocks.",
        )

    st.subheader("3️⃣ Optimize")
    if st.button("🚀 Optimize", type="primary", width="stretch"):
        run_pipeline(tickers, amount, risk_label, budget, cfg)

    # Results live in session_state so they survive Streamlit's reruns
    # (slider wiggles, window resizes, …) until the next optimization.
    if "result" in st.session_state:
        render_results(st.session_state["result"])


if __name__ == "__main__":
    main()
