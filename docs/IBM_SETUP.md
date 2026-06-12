# 🔌 Running every calculation on a real IBM Quantum computer

This guide gets the app running **on your own machine** while sending **all the
quantum optimization to IBM's real quantum hardware** over the cloud — no local
simulator, no IBM hardware in your house, just an API key.

It takes about 10 minutes. You need: a web browser, an email address, and the
app installed (`pip install -r requirements.txt`).

---

## Part 1 — Create your IBM Quantum account and credentials

You need **two strings** from IBM: an **API key** and an **instance CRN**.

### Step 1 — Sign up

1. Go to **https://quantum.cloud.ibm.com**.
2. Click **Sign up / Create account** and register (you can use an IBMid,
   Google, or GitHub login). It is **free** — the default **Open Plan** costs
   nothing and gives you **~10 minutes of real QPU time per 28-day window**.

### Step 2 — Copy your API key

1. After logging in you land on the **Home / dashboard** page.
2. Find the **API key** section and click **Create API key** (or copy the one
   shown).
3. **Copy it immediately and save it somewhere safe** — it is a 44-character
   string and IBM shows it **only once**. If you lose it, just create another.

### Step 3 — Copy your instance CRN

The new IBM Quantum Platform runs your jobs inside an **instance**, identified
by a **CRN** (Cloud Resource Name).

1. Open the **Instances** page: **https://quantum.cloud.ibm.com/instances**.
2. You should already have a free Open-Plan instance. If the list is empty,
   click **Create instance**, pick the **Open** plan, and create it. (Open-Plan
   instances live in the **us-east** region — you can switch region from the
   drop-down at the top of the dashboard if needed.)
3. **Hover over the instance's CRN** and click the **copy** icon. Save it next
   to your API key. A CRN looks like:
   ```
   crn:v1:bluemix:public:quantum-computing:us-east:a/abc123...:guid::
   ```

You now have everything: **API key** + **instance CRN**. ✅

---

## Part 2 — Give the credentials to the app

Pick **one** of these three ways (any one is enough).

### Option A — Paste them into the app (quickest, nothing saved to disk)

1. Start the app: `streamlit run app.py`
2. In the left sidebar, set **"Where should the quantum circuits run?"** to
   **IBM Quantum hardware**.
3. Paste your **API key** into *IBM Quantum API key* and your **CRN** into
   *Instance CRN*.
4. Click **🔌 Test IBM connection**. You should see a green
   *"Connected — N backend(s) available"* with a list of devices. (This step
   does **not** use any quota — it only lists machines.)

### Option B — A `.env` file (so you don't paste every time)

In the project folder:

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```
IBM_QUANTUM_TOKEN=your-44-character-api-key
IBM_QUANTUM_INSTANCE=crn:v1:bluemix:public:quantum-computing:us-east:a/...::
```

The app loads `.env` automatically on start. Leave the sidebar fields blank;
the **Test connection** button will use these values.

### Option C — Save the account into Qiskit once (machine-wide)

Run this **once** in a Python shell (in the project's virtualenv):

```python
from qiskit_ibm_runtime import QiskitRuntimeService

QiskitRuntimeService.save_account(
    channel="ibm_quantum_platform",
    token="your-44-character-api-key",
    instance="crn:v1:bluemix:public:quantum-computing:us-east:a/...::",
    set_as_default=True,
    overwrite=True,
)
```

This writes `~/.qiskit/qiskit-ibm.json`. From then on the app finds your
credentials with the sidebar left blank — no env vars, no pasting.

---

## Part 3 — Force every calculation onto the quantum computer

By default the app falls back to the local simulator if IBM is unreachable. To
guarantee **100% of the optimization runs on IBM hardware**:

1. Sidebar → **IBM Quantum hardware** selected.
2. **Uncheck** *"Fall back to the local simulator if IBM is unavailable"*.
   Now, if IBM can't be reached, the run **errors** instead of quietly
   computing locally — so you know nothing ran on a simulator.
3. Click **🚀 Optimize**.

The result banner names the exact device it ran on, e.g.
*"Optimized with QAOA on the IBM Quantum — ibm_torino (133 qubits)"*.

---

## Part 4 — Settings that respect the free quota (important!)

Real quantum hardware is **slow and rationed**, in two ways:

- **Queue:** your jobs wait in line behind every other Open-Plan user — minutes,
  sometimes longer, per job.
- **Quota:** the free Open Plan gives only **~10 minutes of QPU time per 28
  days**, and **every optimizer iteration is a separate job**.

A 500-stock run does **many** QAOA subproblems × many iterations each = far more
jobs than the free plan allows. So for an all-hardware run, **stay small**:

| Setting (sidebar) | Recommended for IBM hardware | Why |
|---|---|---|
| Number of tickers entered | **4 – 8** | Keeps it to a *single* QAOA optimization, not a tournament. |
| Quantum pool size | = your ticker count | No screening needed at this size. |
| Qubits per subproblem (chunk size) | ≥ your ticker count | One chunk = one quantum problem. |
| QAOA circuit depth (reps) | **1** | Shorter circuits survive hardware noise better. |
| Optimizer iterations (COBYLA) | **5 – 15** | Each iteration is one queued job. |
| Shots per circuit | 1024 – 2048 | Enough statistics without waste. |
| Time budget before fallback | large (e.g. 3600 s) | Lets a queued job actually run. |

> **Tip:** prototype with the **local simulator** (instant, free, unlimited),
> then switch to **IBM Quantum hardware** for one small "real quantum" run.
> The math is identical — only the executor changes.

---

## Troubleshooting

| Message | Fix |
|---|---|
| "No IBM Quantum credentials found" | You didn't supply a key. Paste it in the sidebar, set `IBM_QUANTUM_TOKEN`, or use `save_account` (Part 2). |
| "Could not select a backend … no device with enough qubits" | Lower the **chunk size**, or your instance has no system that large. Use **Test connection** to see what's available. |
| "Connected, but no operational backends" | Your instance has no quantum system attached. Re-create a free **Open** instance on the Instances page. |
| Stuck on the spinner for a long time | Normal — your job is **queued** on a shared device. Raise the *Time budget* and wait, or try a quieter backend (check the **Queue** column in Test connection). |
| Result says **classical fallback** | IBM timed out or errored and fallback was on. Uncheck fallback to force hardware, raise the time budget, or pick a less busy device. |
| "Invalid CRN" / authentication errors | Re-copy the CRN from the Instances page (hover → copy icon) and the API key from the Home dashboard. The key is shown only once; create a new one if unsure. |

---

## What "all calculations on IBM" really means here

The portfolio pipeline is **hybrid** by design: the **stock-selection** step
(the hard combinatorial problem) is what QAOA runs **on the IBM quantum
processor**. The final step — turning the chosen stocks into exact percentages
— is a small continuous optimization that always runs classically on your
machine, because that part isn't a quantum problem and running it on hardware
would waste your quota for no benefit. The result's *What happened* tab states
exactly which device ran the quantum step and how many quantum jobs it used.

Sources: IBM Quantum Platform documentation —
[Initialize your account](https://quantum.cloud.ibm.com/docs/en/guides/initialize-account),
[Save your credentials](https://quantum.cloud.ibm.com/docs/en/guides/save-credentials),
[Plans overview](https://quantum.cloud.ibm.com/docs/en/guides/plans-overview).
