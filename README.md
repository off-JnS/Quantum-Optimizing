# ⚛️ Quantum Portfolio Optimizer

A single-file Streamlit app that picks an investment portfolio with a **real quantum
algorithm (QAOA)** running on a **local quantum simulator** — no IBM account, no API
keys, no paid services.

Type in a few stock tickers and an amount in euros; the app downloads one year of
real market data, lets a quantum algorithm decide *which* stocks deserve your money,
sizes the positions classically, and shows you the result as a pie chart, an
allocation table, and an efficient-frontier plot.

> **Educational demo — not financial advice.**

## Features

- 📈 Real historical data — 1 year of daily prices from Yahoo Finance (via `yfinance`)
- ⚛️ Quantum stock selection — QAOA on qiskit-finance's `PortfolioOptimization`
  QUBO, executed on the local Qiskit **Aer** simulator
- 🎚️ Risk tolerance slider (low / medium / high) that drives the return-vs-risk
  trade-off in *both* the quantum and classical stages
- 🥧 Results: allocation pie chart, per-stock table (%, €, expected return
  contribution), efficient frontier with your portfolio highlighted, and headline
  metrics (expected annual return, volatility, Sharpe)
- 🛟 Robust fallback: if QAOA fails or exceeds its time budget, a classical solver
  takes over and the result is clearly labeled **classical fallback**
- 🗣️ A plain-language box explaining honestly what the quantum algorithm did
- 🌙 Clean dark theme, mobile-friendly layout, cached data fetches

## Quickstart

Requires **Python 3.10 – 3.12** on Windows, macOS, or Linux.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv

#    Windows:
.venv\Scripts\activate
#    macOS / Linux:
source .venv/bin/activate

# 2. Install the pinned dependencies
pip install -r requirements.txt

# 3. Run the app (opens in your browser)
streamlit run app.py
```

The only network access the app ever needs is the Yahoo Finance download when you
press **Optimize** — everything else, including the quantum simulation, runs
entirely on your machine.

## How it works

1. **Measure the market.** One year of daily closes per ticker → annualized expected
   returns **μ** and covariance matrix **Σ**.
2. **Quantum stage — which stocks?** Holding/not-holding each stock is one binary
   decision (one **qubit** per ticker). qiskit-finance's `PortfolioOptimization`
   turns "maximize `μ·x − q·(x·Σ·x)` while holding exactly *B* stocks" into a QUBO,
   and **QAOA** (a variational quantum algorithm) searches the 2ⁿ possible
   combinations on the Aer simulator. The risk slider sets the risk-aversion *q*.
3. **Classical stage — how much of each?** Within the chosen stocks, a classical
   SLSQP optimizer maximizes the same objective over continuous long-only weights
   summing to 1. This split is shown as your allocation.
4. **Fallbacks.** If QAOA times out or errors, the *same* QUBO is solved exactly by
   a classical eigensolver (instant at ≤ 8 stocks). If even that fails, you get a
   classical minimum-variance portfolio. Both cases are labeled prominently.

## Limitations (a.k.a. honesty corner)

- **Max 8 tickers** — each stock is one qubit and a laptop-grade simulator slows
  down quickly beyond that. This is a hardware-era limitation of the demo, not of
  the math.
- The quantum advantage here is **illustrative**: with ≤ 8 stocks a classical
  computer checks all combinations instantly. QAOA's promise is at scales where
  2ⁿ enumeration becomes impossible.
- Expected returns are estimated from one year of history — the standard caveat
  that past performance does not predict future results very much applies.
- A QAOA run abandoned by the timeout finishes quietly in the background (its
  iterations are bounded); this is harmless.

## Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| "No price data found for: XYZ" | The ticker doesn't exist on Yahoo Finance. Non-US listings need an exchange suffix: `SAP.DE` (Xetra), `AIR.PA` (Paris), `7203.T` (Tokyo). |
| "Excluded XYZ: fewer than 60 days of price history" | The ticker is valid but too new (recent IPO) or too thinly traded for meaningful statistics — it's left out so it doesn't shrink the usable history of your other stocks. |
| Yahoo Finance download fails or hangs | Yahoo rate-limits aggressively. Wait a minute and retry — successful fetches are cached for an hour, so re-optimizing the same tickers won't re-download. |
| Result says **classical fallback** | The quantum run failed or exceeded its time budget; the answer shown was computed classically (and exactly). Raise the time budget or lower shots/depth in ⚙️ Advanced settings to try quantum again. |
| First optimization feels slow | The first QAOA run compiles circuits and warms up Aer; subsequent runs are faster. |
| `pip install` fails on Python 3.13 | Use Python 3.10 – 3.12; the pinned scientific stack targets those versions. |

## Verifying the install (optional)

```bash
python scripts/smoke_test.py
```

Runs the full pipeline headlessly on synthetic data: the QAOA selection, the
timeout-fallback path, the weighting stage, and the yfinance response-shape
handling. Every line should say `PASS` (the live-fetch check may `SKIP` offline).

## Tested versions

| Package | Version |
|---|---|
| qiskit | 2.4.1 |
| qiskit-aer | 0.17.2 |
| qiskit-optimization | 0.7.0 |
| qiskit-finance | 0.4.1 |
| qiskit-algorithms | 0.4.0 |
| streamlit | 1.58.0 |
| yfinance | 1.4.1 |
| plotly | 6.8.0 |
| pandas / numpy / scipy | 2.3.3 / 2.2.6 / 1.15.3 |

> Note for qiskit veterans: since qiskit-optimization 0.7.0 the QAOA used with
> `MinimumEigenOptimizer` is the one vendored in
> `qiskit_optimization.minimum_eigensolvers` (qiskit-algorithms remains installed
> as a qiskit-finance dependency).
