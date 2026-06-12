# ⚛️ Quantum Portfolio Optimizer

A Streamlit web app that builds stock portfolios with a **real quantum
algorithm (QAOA)** — running on **IBM Quantum hardware via the cloud API** or
on a local simulator — for universes of **up to 500 stocks**.

Paste tickers, set an amount in euros and a risk tolerance; the app downloads
a year of real market data, screens the field, lets QAOA pick the stocks
through a chunked quantum tournament, sizes the positions classically, and
explains every step in plain language.

> **Educational demo — not financial advice.**

---

## Highlights

- ⚛️ **Real quantum hardware**: one click switches between IBM Quantum
  (API key) and the bundled Aer simulator (free, no account)
- 📈 **Up to 500 tickers**: classical screening + hierarchical QAOA tournament
  keep every quantum subproblem within today's qubit counts
- 🛟 **Honest fallbacks**: timeout or failure → classical solver, always
  clearly labeled; nothing fails silently
- 📊 **Full results**: allocation pie + table + CSV export, efficient
  frontier, expected return / volatility / Sharpe, plain-language report
- 🐳 **Deploy-ready**: Dockerfile + compose + auto-HTTPS proxy — push the repo
  to a Hostinger VPS and it runs 24/7

## Project structure

```
├── app.py                      # Streamlit UI (entry point)
├── quantum_portfolio/          # core package
│   ├── data.py                 #   tickers, Yahoo Finance, validation, statistics
│   ├── engines.py              #   IBM Quantum / local Aer execution engines
│   └── optimizer.py            #   screening, QAOA tournament, fallbacks, weighting
├── scripts/smoke_test.py       # 17 headless checks (run after install)
├── docs/
│   ├── USAGE.md                # 📖 full manual: every control, IBM setup, scaling
│   └── DEPLOY_HOSTINGER.md     # 🚀 host it 24/7 on a Hostinger VPS from GitHub
├── Dockerfile                  # production image
├── docker-compose.yml          # app + Caddy reverse proxy (auto-HTTPS)
├── deploy/Caddyfile
├── .streamlit/config.toml      # dark theme
└── requirements.txt            # exact, mutually verified pins
```

## Quickstart (local)

Python 3.10 – 3.12 on Windows, macOS or Linux:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Or with Docker: `docker compose up -d` → http://localhost

## Using IBM Quantum hardware

1. Create a free account at [quantum.cloud.ibm.com](https://quantum.cloud.ibm.com)
   and generate an **API key**.
2. Paste it in the app's sidebar — or set `IBM_QUANTUM_TOKEN` in the
   environment (the right way on a server).
3. Pick *IBM Quantum hardware* in the sidebar and optimize. The app chooses
   the least busy device automatically and falls back to the local simulator
   (with a visible warning) if IBM is unreachable.

⏱️ The free Open plan includes ~10 minutes of QPU time per month and public
devices queue — see [docs/USAGE.md](docs/USAGE.md) for quota-friendly settings.

## How it scales to 500 stocks

One qubit per stock per subproblem is the hard physical constraint, so the
optimizer is hybrid: a classical pre-screen reduces the universe to a quantum
pool (default 32), then a **tournament of QAOA subproblems** (chunks of 4–32
qubits) eliminates candidates round by round until exactly your requested
number of stocks remains. Positions are then sized classically with the same
gain-versus-risk objective. The result banner reports how many quantum
subproblems ran, on which backend, and at what size — and the *What happened*
tab explains it all without jargon.

## Hosting it 24/7 on Hostinger

The repo deploys as-is on a Hostinger **VPS** (shared hosting can't run
Python servers): hPanel → Docker Manager → paste the repo URL → set
`IBM_QUANTUM_TOKEN` and `DOMAIN` → deploy. Caddy provisions HTTPS
automatically and Docker restarts the app after crashes and reboots.

**Step-by-step guide: [docs/DEPLOY_HOSTINGER.md](docs/DEPLOY_HOSTINGER.md)**

## Verifying an install

```bash
python scripts/smoke_test.py     # 17 checks, all should PASS
```

## Tested versions

| Package | Version |
|---|---|
| qiskit | 2.4.1 |
| qiskit-aer | 0.17.2 |
| qiskit-ibm-runtime | 0.47.0 |
| qiskit-optimization / -finance / -algorithms | 0.7.0 / 0.4.1 / 0.4.0 |
| streamlit / yfinance / plotly | 1.58.0 / 1.4.1 / 6.8.0 |
| pandas / numpy / scipy | 2.3.3 / 2.2.6 / 1.15.3 |

Python 3.10 – 3.12 · Windows, macOS, Linux · No paid APIs; the only network
calls are Yahoo Finance downloads and (optionally) IBM Quantum jobs.
