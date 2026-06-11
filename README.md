# ⚛️ Quantum Portfolio Optimizer

A Streamlit app that picks an investment portfolio with a **real quantum
algorithm (QAOA)** — on a local simulator _or_ on a **real IBM quantum computer**.

Type in up to **500 stock tickers** and an amount in euros; the app downloads
one year of real market data, lets a quantum algorithm decide _which_ stocks
(or groups of stocks) deserve your money, sizes the positions classically, and
shows you the result as a pie chart, an allocation table, an efficient-frontier
plot, and a plain-language explanation.

> **Educational demo — not financial advice.**

## Features

- 📈 **Real historical data** — 1 year of daily prices from Yahoo Finance
- ⚛️ **Quantum stock selection** — QAOA on qiskit-finance's `PortfolioOptimization`
  QUBO, executed on the **local Aer simulator** or on **real IBM Quantum hardware**
- 🧩 **Up to 500 stocks** — K-Means hierarchical clustering groups large universes
  into ≤ 20 "super-stocks"; QAOA selects which clusters to hold, then SLSQP
  distributes weights to individual stocks within each cluster
- 🎚️ Risk tolerance slider (low / medium / high)
- 🥧 Results: allocation pie chart (top-25 positions), full allocation table with
  cluster assignments, efficient frontier, and headline metrics
- 🛟 **Robust fallback**: if QAOA fails or exceeds its time budget, a classical
  solver takes over and the result is clearly labeled
- 🌙 Clean dark theme, mobile-friendly layout, cached data fetches
- 🐳 **Docker-ready** for one-command deployment on any Linux VPS

## Quickstart (local)

Requires **Python 3.10 – 3.12** on Windows, macOS, or Linux.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
#    Windows:    .venv\Scripts\activate
#    macOS/Linux: source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) Configure IBM Quantum — copy .env.example to .env and fill in
cp .env.example .env
# then edit .env and set IBM_QUANTUM_TOKEN=<your token from quantum.ibm.com>

# 4. Run the app
streamlit run app.py
```

## IBM Quantum hardware

To run QAOA on a real IBM quantum computer:

1. Create a free account at [quantum.ibm.com](https://quantum.ibm.com/)
2. Copy your API token from **Account → API token**
3. Set it in your environment:

```bash
# In .env (recommended — never commit this file):
IBM_QUANTUM_TOKEN=your_token_here
# IBM_QUANTUM_BACKEND=ibm_sherbrooke   # optional: pin a specific device

# Or export directly:
export IBM_QUANTUM_TOKEN=your_token_here
```

The app will display a green banner when a valid IBM token is detected and will
automatically pick the **least-busy** real quantum device with enough qubits,
falling back to the local Aer simulator on any connection error.

## How it works

### For ≤ 20 stocks (direct mode)
1. **Measure the market.** 1 year of daily closes → annualized **μ** and **Σ**.
2. **Quantum stage.** Each stock is one binary qubit. QAOA searches the 2ⁿ
   combinations to pick the best *B* stocks.
3. **Classical stage.** SLSQP maximizes the same utility over continuous weights
   within the selected stocks.

### For 21–500 stocks (hierarchical mode)
1. **Measure the market** (same as above).
2. **Cluster.** K-Means groups all *n* stocks into *K* ≤ 20 clusters by return &
   risk similarity.
3. **Quantum stage.** QAOA runs on *K* qubits (cluster representatives) and picks
   *B* clusters to hold.
4. **Classical sizing.** SLSQP allocates weights at the cluster level, then again
   within each selected cluster for individual stock positions.

## Deploying to Hostinger (24/7 website)

### Option A — Docker (recommended, any Hostinger VPS / Cloud)

```bash
# On your Hostinger VPS (Ubuntu 22.04):
git clone https://github.com/off-JnS/Quantum-Optimizing.git
cd Quantum-Optimizing
cp .env.example .env
nano .env            # paste your IBM_QUANTUM_TOKEN if desired

# Build and start (stays running forever via restart: unless-stopped)
docker compose up -d --build

# The app is now live on port 8501.
# Point Nginx at it (see deploy/nginx.conf) for HTTPS on your domain.
```

### Option B — Systemd service (bare-metal Hostinger VPS)

Run the one-shot setup script on your freshly provisioned Ubuntu 22.04 VPS:

```bash
# On your local machine — upload the repo to the server, then:
DOMAIN=mysite.com bash deploy/setup.sh
```

The script:
1. Installs Python 3.11, Nginx, Certbot, and all Python dependencies
2. Creates a `quantumapp` system user
3. Installs a **systemd service** (`quantum-optimizer.service`) that auto-restarts
   on crash and starts on boot
4. Configures **Nginx** as a reverse proxy (with WebSocket support for Streamlit)
5. Obtains a free **Let's Encrypt SSL certificate** for your domain

After setup:
```bash
# Check service health
systemctl status quantum-optimizer

# View live logs
journalctl -u quantum-optimizer -f

# Add / update IBM Quantum token and restart
nano /opt/quantum-optimizer/.env
systemctl restart quantum-optimizer
```

### Hostinger VPS requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| Plan | KVM 2 (2 vCPU, 8 GB RAM) | KVM 4 (4 vCPU, 16 GB RAM) |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Disk | 20 GB SSD | 40 GB SSD |
| Python | 3.11 | 3.11 |

> **RAM note:** The quantum packages (Qiskit Aer, NumPy, SciPy) and large covariance
> matrices for 500 stocks require ~3–5 GB RAM during optimization. A minimum
> 8 GB RAM VPS plan is strongly recommended.

## Troubleshooting

| Symptom | What to do |
|---|---|
| "No price data found for: XYZ" | Invalid ticker — non-US stocks need exchange suffix: `SAP.DE`, `AIR.PA`, `7203.T` |
| "Excluded XYZ: fewer than 60 days" | Recent IPO or thinly traded — not enough history |
| Yahoo Finance fails / hangs | Rate-limited — wait 1 min and retry (fetches are cached 1 h) |
| **Classical fallback** shown | QAOA timed out; raise the time budget in ⚙️ Advanced settings |
| IBM Quantum: connection failed | Check token, internet, and that `qiskit-ibm-runtime` is installed |
| `pip install` fails on Python 3.13 | Use Python 3.10–3.12 |

## Verifying the install

```bash
python scripts/smoke_test.py
```

Runs the full pipeline headlessly on synthetic data (no network needed).
Every line should print `PASS`.

## Tested versions

| Package | Version |
|---|---|
| qiskit | 2.4.1 |
| qiskit-aer | 0.17.2 |
| qiskit-optimization | 0.7.0 |
| qiskit-finance | 0.4.1 |
| qiskit-algorithms | 0.4.0 |
| qiskit-ibm-runtime | ≥ 0.34.0 |
| scikit-learn | ≥ 1.5.0 |
| streamlit | 1.45.1 |
| yfinance | 1.4.1 |
| plotly | 6.8.0 |
| pandas / numpy / scipy | 2.3.3 / 2.2.6 / 1.15.3 |
