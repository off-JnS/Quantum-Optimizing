"""Optimization layer: screening, hierarchical QAOA selection, fallbacks, weighting.

Scaling idea (how 500 stocks fit on a ~100-qubit machine):

1. **Screen** — rank every stock by its individual risk-adjusted score
   μᵢ − q·Σᵢᵢ and keep the best ``pool_size`` candidates ("the quantum pool").
2. **Tournament** — split the pool into chunks that fit the backend
   (``chunk_size`` qubits each), let QAOA pick the winners of each chunk via
   qiskit-finance's PortfolioOptimization QUBO, and repeat with the survivors
   until one final chunk remains; a last QAOA run selects exactly ``budget``
   stocks. Every subproblem is a genuine quantum optimization over the full
   covariance block of its chunk.
3. **Weight** — classical SLSQP sizes the positions within the selected stocks
   using the same utility μ·w − q·(w·Σ·w).

Each stage degrades gracefully: if QAOA fails or exceeds its time budget, the
same tournament runs with an exact/greedy classical chunk solver; if even that
fails, a minimum-variance portfolio over the screened pool is returned. The
result is always labeled with how it was obtained.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from itertools import combinations
from math import comb

import numpy as np
from scipy.optimize import minimize

from quantum_portfolio.engines import QuantumEngine

# Risk tolerance -> risk-aversion coefficient q in the objective  max  μ·w − q·(w·Σ·w).
# NOTE: calibrated for ANNUALIZED μ and Σ (daily stats × 252); rescale q if you
# change the annualization.
RISK_MAP = {"Low": 2.0, "Medium": 1.0, "High": 0.25}

BRUTE_FORCE_LIMIT = 200_000  # max C(n, k) for exact classical chunk enumeration
SEED = 42


# ==============================================================================
# Stage 0 — classical screening
# ==============================================================================

def screen_universe(mu: np.ndarray, sigma: np.ndarray, q: float, pool_size: int) -> np.ndarray:
    """Indices of the ``pool_size`` stocks with the best individual
    risk-adjusted score μᵢ − q·Σᵢᵢ. Diversification across the survivors is the
    tournament's job (it sees the full covariance block of every chunk)."""
    n = len(mu)
    if n <= pool_size:
        return np.arange(n)
    score = mu - q * np.diag(sigma)
    return np.sort(np.argsort(score)[::-1][:pool_size])


# ==============================================================================
# Chunk solvers (quantum and classical) — pick exactly k stocks out of a chunk
# ==============================================================================

def qaoa_chunk_select(
    mu: np.ndarray,
    sigma: np.ndarray,
    q: float,
    k: int,
    engine: QuantumEngine,
    *,
    reps: int,
    maxiter: int,
    seed: int,
) -> np.ndarray:
    """One quantum subproblem: QAOA on qiskit-finance's portfolio QUBO
    (maximize μ·x − q·x·Σ·x subject to Σx = k, x binary)."""
    from qiskit_finance.applications.optimization import PortfolioOptimization
    from qiskit_optimization.algorithms import MinimumEigenOptimizer
    from qiskit_optimization.minimum_eigensolvers import QAOA
    from qiskit_optimization.optimizers import COBYLA
    from qiskit_optimization.utils import algorithm_globals

    algorithm_globals.random_seed = seed
    qp = PortfolioOptimization(
        expected_returns=mu, covariances=sigma, risk_factor=q, budget=k
    ).to_quadratic_program()
    qaoa = QAOA(
        sampler=engine.sampler,
        optimizer=COBYLA(maxiter=maxiter),
        reps=reps,
        pass_manager=engine.pass_manager,
    )
    result = MinimumEigenOptimizer(qaoa).solve(qp)
    selection = np.asarray(np.round(result.x), dtype=int)
    if result.status.name == "SUCCESS" and int(selection.sum()) == int(k):
        return selection
    # Shot noise can make the single lowest-energy sample violate the budget
    # constraint even when plenty of feasible samples were measured — rescue
    # the best feasible one before declaring the quantum run a failure.
    samples = sorted(getattr(result, "samples", None) or [], key=lambda s: s.fval)
    for sample in samples:
        x = np.asarray(np.round(sample.x), dtype=int)
        if int(x.sum()) == int(k):
            return x
    raise RuntimeError(f"QAOA produced no measurement holding exactly {k} stocks")


def classical_chunk_select(mu: np.ndarray, sigma: np.ndarray, q: float, k: int) -> np.ndarray:
    """Classical chunk solver: exact enumeration when feasible, otherwise
    greedy construction refined by pairwise swaps. Used for the fallback path."""
    n = len(mu)
    if k >= n:
        return np.ones(n, dtype=int)

    def utility(idx: list[int]) -> float:
        x = np.zeros(n)
        x[list(idx)] = 1.0
        return float(x @ mu - q * (x @ sigma @ x))

    if comb(n, k) <= BRUTE_FORCE_LIMIT:
        best = max(combinations(range(n), k), key=lambda c: utility(list(c)))
        chosen = list(best)
    else:
        chosen: list[int] = []
        rest = set(range(n))
        for _ in range(k):  # greedy forward selection
            nxt = max(rest, key=lambda j: utility(chosen + [j]))
            chosen.append(nxt)
            rest.remove(nxt)
        improved = True
        while improved:  # 1-swap local search
            improved = False
            for pos in range(k):
                for j in list(rest):
                    candidate = chosen.copy()
                    candidate[pos] = j
                    if utility(candidate) > utility(chosen) + 1e-12:
                        rest.add(chosen[pos])
                        chosen = candidate
                        rest.remove(j)
                        improved = True
    mask = np.zeros(n, dtype=int)
    mask[chosen] = 1
    return mask


# ==============================================================================
# Stage 1 — hierarchical tournament over the screened pool
# ==============================================================================

def tournament_select(
    mu: np.ndarray,
    sigma: np.ndarray,
    q: float,
    budget: int,
    chunk_size: int,
    chunk_solver,
    seed: int = SEED,
) -> tuple[np.ndarray, dict]:
    """Select exactly ``budget`` of n stocks using subproblems of at most
    ``chunk_size`` variables (= qubits, for the quantum solver).

    Elimination rounds halve the field chunk by chunk until it fits in a single
    chunk, then a final run picks the portfolio. When the requested budget is
    close to (or above) the remaining field, the budget is distributed across
    chunks proportionally instead — so the subproblem size NEVER exceeds
    ``chunk_size`` and the selection always sums to ``budget``.
    """
    n = len(mu)
    stats = {"subproblems": 0, "rounds": 0, "max_chunk": 0}
    if budget >= n:
        return np.ones(n, dtype=int), stats

    rng = np.random.default_rng(seed)

    def solve(indices: np.ndarray, k: int) -> np.ndarray:
        """Run the chunk solver on a subset; returns the chosen global indices."""
        if k >= len(indices):
            return indices
        mask = chunk_solver(mu[indices], sigma[np.ix_(indices, indices)], q, int(k))
        stats["subproblems"] += 1
        stats["max_chunk"] = max(stats["max_chunk"], len(indices))
        return indices[mask.astype(bool)]

    def chunks_of(indices: np.ndarray) -> list[np.ndarray]:
        return [
            indices[start:start + chunk_size]
            for start in range(0, len(indices), chunk_size)
        ]

    def proportional_select(indices: np.ndarray, k: int) -> np.ndarray:
        """Distribute a budget of k across chunks by largest remainder."""
        parts = chunks_of(indices)
        exact = np.array([k * len(c) / len(indices) for c in parts])
        quotas = np.floor(exact).astype(int)
        remainder = k - quotas.sum()
        for i in np.argsort(exact - quotas)[::-1][:remainder]:
            quotas[i] += 1
        chosen = [solve(c, int(b)) for c, b in zip(parts, quotas) if b > 0]
        return np.concatenate(chosen) if chosen else np.array([], dtype=int)

    pool = np.arange(n)
    while len(pool) > chunk_size:
        rng.shuffle(pool)
        if 2 * budget >= len(pool):
            # Budget close to the field size: halving would dip below it, so
            # finish with one proportional round instead.
            pool = proportional_select(pool, budget)
            stats["rounds"] += 1
            break
        stats["rounds"] += 1
        survivors = [solve(c, int(np.ceil(len(c) / 2))) for c in chunks_of(pool)]
        pool = np.concatenate(survivors)
    else:
        pool = solve(pool, budget) if budget < len(pool) else pool

    if len(pool) > budget:  # proportional round may overshoot only via solve(k>=len)
        pool = solve(np.sort(pool), budget)

    selection = np.zeros(n, dtype=int)
    selection[pool] = 1
    return selection, stats


# ==============================================================================
# Stage 2 — classical weighting
# ==============================================================================

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
    """Continuous long-only weights within the selected stocks, maximizing the
    same utility  μ·w − q·(w·Σ·w)  with weights summing to 1."""
    return _long_only_solve(
        lambda w: -(mu_sel @ w - q * (w @ sigma_sel @ w)), len(mu_sel)
    )


def min_variance_weights(sigma: np.ndarray) -> np.ndarray:
    """Last-resort classical fallback: long-only minimum-variance portfolio
    (ignores expected returns entirely)."""
    return _long_only_solve(lambda w: w @ sigma @ w, len(sigma))


# ==============================================================================
# Orchestration: quantum with timeout -> classical fallback -> min variance
# ==============================================================================

def _quantum_selection(mu, sigma, q, budget, engine, chunk_size, *, reps, maxiter, seed):
    return tournament_select(
        mu, sigma, q, budget, chunk_size,
        chunk_solver=lambda m, s, qq, k: qaoa_chunk_select(
            m, s, qq, k, engine, reps=reps, maxiter=maxiter, seed=seed
        ),
        seed=seed,
    )


def selection_with_fallback(
    mu: np.ndarray,
    sigma: np.ndarray,
    q: float,
    budget: int,
    engine: QuantumEngine,
    chunk_size: int,
    *,
    timeout_s: float,
    reps: int,
    maxiter: int,
    seed: int,
) -> tuple[np.ndarray | None, dict, str, str | None]:
    """Try the quantum tournament with a hard time budget; fall back to the
    same tournament with a classical chunk solver.

    Returns (selection, stats, method, fallback_reason) where method is one of
    "quantum", "classical" or "min_variance" (caller computes min-var weights).
    """
    # Deliberately NOT a `with` block: ThreadPoolExecutor.__exit__ would join the
    # still-running QAOA worker and defeat the timeout. shutdown(wait=False)
    # abandons it instead; the bounded shots/maxiter guarantee it terminates.
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        _quantum_selection, mu, sigma, q, budget, engine, chunk_size,
        reps=reps, maxiter=maxiter, seed=seed,
    )
    try:
        selection, stats = future.result(timeout=timeout_s)
        return selection, stats, "quantum", None
    except FuturesTimeout:
        reason = (
            f"the quantum run did not finish within its {timeout_s:.0f} s time "
            f"budget on the {engine.label}"
        )
    except Exception as exc:
        reason = f"the quantum run failed ({exc})"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    try:
        selection, stats = tournament_select(
            mu, sigma, q, budget, chunk_size,
            chunk_solver=classical_chunk_select, seed=seed,
        )
        return selection, stats, "classical", reason
    except Exception as exc:
        reason = f"{reason}; the classical solver also failed ({exc})"
        return None, {"subproblems": 0, "rounds": 0, "max_chunk": 0}, "min_variance", reason


def optimize_portfolio(
    mu: np.ndarray,
    sigma: np.ndarray,
    q: float,
    budget: int,
    engine: QuantumEngine,
    *,
    pool_size: int,
    chunk_size: int,
    reps: int,
    maxiter: int,
    timeout_s: float,
    seed: int,
) -> dict:
    """Full pipeline: screen -> quantum tournament selection -> classical sizing."""
    t0 = time.perf_counter()
    n = len(mu)
    pool_size = max(pool_size, budget)          # the pool must be able to hold the budget
    chunk_size = min(chunk_size, engine.max_qubits)

    pool_idx = screen_universe(mu, sigma, q, pool_size)
    mu_pool, sigma_pool = mu[pool_idx], sigma[np.ix_(pool_idx, pool_idx)]

    pool_selection, stats, method, reason = selection_with_fallback(
        mu_pool, sigma_pool, q, min(budget, len(pool_idx)), engine, chunk_size,
        timeout_s=timeout_s, reps=reps, maxiter=maxiter, seed=seed,
    )

    selection = np.zeros(n, dtype=int)
    weights = np.zeros(n)
    if pool_selection is None:  # method == "min_variance"
        selection[pool_idx] = 1
        weights[pool_idx] = min_variance_weights(sigma_pool)
    else:
        chosen_global = pool_idx[pool_selection.astype(bool)]
        selection[chosen_global] = 1
        weights[chosen_global] = slsqp_weights(
            mu[chosen_global], sigma[np.ix_(chosen_global, chosen_global)], q
        )

    exp_return = float(weights @ mu)
    volatility = float(np.sqrt(max(weights @ sigma @ weights, 0.0)))
    return {
        "method": method,
        "fallback_reason": reason,
        "engine_label": engine.label,
        "engine_kind": engine.kind,
        "selection": selection,
        "weights": weights,
        "pool_indices": pool_idx,
        "screened": bool(len(pool_idx) < n),
        "stats": stats,
        "exp_return": exp_return,
        "volatility": volatility,
        "sharpe": exp_return / volatility if volatility > 0 else 0.0,
        "elapsed_s": time.perf_counter() - t0,
    }


def random_portfolios(
    mu: np.ndarray, sigma: np.ndarray, n_portfolios: int, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized cloud of random long-only portfolios for the frontier chart."""
    rng = np.random.default_rng(seed)
    w = rng.dirichlet(np.ones(len(mu)), n_portfolios)
    rets = w @ mu
    vols = np.sqrt(((w @ sigma) * w).sum(axis=1))
    return rets, vols
