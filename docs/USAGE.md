# 📖 Usage Guide — Quantum Portfolio Optimizer

This is the complete manual. For a 30-second overview, the app itself has a
"📋 How to use" panel at the top.

---

## 1. What the app does

You give it a list of stock tickers (2 – 500), an amount in euros and a risk
tolerance. It then:

1. downloads **one year of real daily prices** for every ticker (Yahoo Finance),
2. **screens** the universe classically down to a "quantum pool" of the best
   risk-adjusted candidates,
3. runs a **hierarchical QAOA tournament** — a series of small quantum
   optimizations (one qubit per stock, chunks sized to fit the quantum
   processor) that decide *which* stocks to hold,
4. **sizes the positions** classically with the same gain-versus-risk objective,
5. shows the allocation (pie + table + CSV export), an efficient-frontier
   chart, and a plain-language report of what the quantum computer actually did.

The quantum stage runs either on **real IBM Quantum hardware** (cloud API) or
on the **local Aer simulator** (free, no account). If the quantum run fails or
exceeds its time budget, a classical solver finishes the job and the result is
clearly labeled **classical fallback**.

---

## 2. Installation (local)

Requires **Python 3.10 – 3.12** (Windows, macOS or Linux).

```bash
git clone https://github.com/off-JnS/Quantum-Optimizing.git
cd Quantum-Optimizing

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

Or with Docker (identical to the production deployment):

```bash
docker compose up -d        # then open http://localhost
```

---

## 3. Getting an IBM Quantum API key

1. Create a free account at **https://quantum.cloud.ibm.com**.
2. Open **Access management → API keys** (or your account settings) and create
   an API key. Copy it immediately — it is shown only once.
3. Give it to the app in one of two ways:
   * **Paste it** into the sidebar field *IBM Quantum API key*, or
   * **Set the environment variable** `IBM_QUANTUM_TOKEN` (the right way on a
     server — see the deployment guide).
4. Optional sidebar fields:
   * **Instance CRN** — only needed if your account has multiple instances and
     you don't want the default one.
   * **Backend name** — e.g. `ibm_torino`; leave empty and the app picks the
     least busy operational device automatically.

### Quota reality check

The free **Open plan** includes roughly **10 minutes of QPU time per month**.
A QAOA run is *iterative* — every optimizer iteration is a separate cloud job —
so the IBM defaults in the sidebar are deliberately frugal (depth 1, ~20
iterations). Practical advice:

* Prototype on the **local simulator**, switch to IBM for the final run.
* Keep **Optimizer iterations** ≤ 20 and **QAOA depth** at 1 on hardware.
* Expect **queueing**: public devices serve many users; the *time budget*
  slider decides how long to wait before the classical fallback takes over.

---

## 4. The interface, control by control

### Sidebar — "⚙️ Quantum engine"

| Control | Meaning |
|---|---|
| Where should the quantum circuits run? | IBM Quantum hardware (cloud) or local Aer simulator. |
| IBM Quantum API key | Your key; leave empty if `IBM_QUANTUM_TOKEN` is set. |
| Instance CRN (optional) | Specific IBM Cloud instance; empty = account default. |
| Backend name (optional) | Specific device; empty = least busy. |
| Fall back to local simulator | If IBM is unreachable (bad key, no quota, network), run locally instead of failing — always with a visible warning. |

### Sidebar — "🔬 Algorithm settings"

| Control | Default | Meaning |
|---|---|---|
| Quantum pool size | 32 | How many stocks survive the classical pre-screen into the quantum stage. |
| Qubits per subproblem | 10 local / 16 IBM | Chunk size of the tournament; one qubit per stock per chunk. |
| QAOA circuit depth (reps) | 2 local / 1 IBM | Deeper = potentially better answers, but longer/noisier circuits. |
| Shots per circuit | 1024 local / 2048 IBM | Measurements per circuit execution. |
| Optimizer iterations (COBYLA) | 150 local / 20 IBM | Variational loop length. **On hardware each iteration is a separate cloud job.** |
| Time budget | 120 s local / 3600 s IBM | Hard deadline before the classical fallback takes over. |
| Random seed | 42 | Reproducibility of shuffling, sampling and simulation. |

### Main area

1. **Stocks** — paste tickers separated by commas, spaces or newlines
   (2 – 500). Yahoo Finance symbols; non-US listings need their exchange
   suffix (`SAP.DE`, `AIR.PA`, `7203.T`). A live counter shows how many were
   recognized.
2. **Investment** — amount in €, risk tolerance (Low / Medium / High), and how
   many stocks the final portfolio should hold (1 – 40).
3. **Optimize** — runs the pipeline. Progress is shown in spinners; results
   persist until the next run.

### Results

* **Status banner** — tells you exactly how the result was produced: quantum
  (with device name, number of subproblems and max qubits), classical
  fallback (with the reason), or minimum-variance last resort.
* **📊 Allocation** — donut chart (small holdings grouped into "Other"), a
  table of every selected stock (allocation %, € amount, return contribution,
  expected €/year), and a CSV download covering *all* tickers.
* **🌌 Efficient frontier** — 3,000 random portfolios built from the screened
  pool; your portfolio is the ⭐. Up and to the left is better.
* **🧠 What happened** — the honest, jargon-free narrative of every stage.

---

## 5. How the 500-stock scaling works (the honest version)

A quantum computer needs **one qubit per stock per subproblem**, and today's
devices have ~100–150 usable qubits (simulators far fewer). The app bridges
the gap with a hybrid strategy:

```
500 tickers
   │  classical screen: rank by μᵢ − q·Σᵢᵢ, keep best `pool size`
   ▼
 32-stock quantum pool
   │  tournament round 1: chunks of ≤`chunk size` qubits,
   │  QAOA keeps the better half of each chunk
   ▼
 16 survivors
   │  final QAOA round (or proportional rounds when the budget
   │  is close to the field size)
   ▼
 exactly `number of stocks to hold` winners
   │  classical SLSQP sizing with the same objective
   ▼
 your allocation
```

Every chunk is a genuine quantum optimization over that chunk's full
covariance block. The status banner reports how many subproblems ran and how
many qubits the largest one used.

---

## 6. Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| "No price data found for: XYZ" | Ticker doesn't exist on Yahoo Finance. Non-US listings need a suffix: `SAP.DE`, `AIR.PA`, `7203.T`. |
| "Excluded XYZ: fewer than 60 days of history" | Valid but too new/thin — left out so it doesn't shrink everyone else's usable history. |
| "No API key yet" warning in the sidebar | Paste a key or set `IBM_QUANTUM_TOKEN`; until then runs use the local simulator. |
| "Could not connect to IBM Quantum: …" | Wrong/expired key, missing instance CRN, or no network. With fallback enabled the run continues locally. |
| Result says **classical fallback** | The quantum run timed out or failed; the same problem was solved classically. On IBM this is usually **queueing** — raise the time budget or pick a quieter backend. |
| Optimization sits at the spinner for a long time (IBM mode) | Normal: cloud jobs queue behind other users. The time budget caps the wait. |
| Yahoo download fails or is slow | Yahoo rate-limits; wait a minute. Successful fetches are cached for an hour. |
| Very concentrated allocation (one or two stocks) | That's the math of maximizing μ·w − q·w·Σ·w when one stock had an extreme year. Choose **Low** risk tolerance to diversify harder. |

---

## 7. Verifying an installation

```bash
python scripts/smoke_test.py
```

Runs 17 headless checks: parsing, data validation, the 500-stock tournament,
real QAOA on the local simulator, the fallback paths and the IBM engine's
error handling. Every line should say `PASS` (the live-fetch check may `SKIP`
when offline).

> **Educational demo — not financial advice.** Expected returns estimated from
> one year of history; past performance does not predict future results.
