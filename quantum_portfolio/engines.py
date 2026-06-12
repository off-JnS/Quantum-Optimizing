"""Quantum execution engines.

Two interchangeable backends drive the QAOA subproblems:

* ``build_local_engine``  — Qiskit Aer simulator on this machine (free, instant,
  no account needed).
* ``build_ibm_engine``    — real IBM Quantum hardware through the cloud API
  (qiskit-ibm-runtime). Needs an API key + instance CRN from
  https://quantum.cloud.ibm.com (see docs/IBM_SETUP.md).

Both return a :class:`QuantumEngine` whose ``sampler``/``pass_manager`` plug
straight into qiskit-optimization's QAOA, so the optimizer code is identical
regardless of where the circuits actually run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class QuantumEngine:
    kind: str          # "local" | "ibm"
    label: str         # human-readable, e.g. "IBM Quantum — ibm_torino (133 qubits)"
    sampler: object    # a BaseSamplerV2 implementation
    pass_manager: object
    max_qubits: int


def resolve_ibm_token(ui_token: str | None) -> str | None:
    """Token precedence: explicit UI input, then the IBM_QUANTUM_TOKEN env var.
    Returns None if neither is set (a saved account may still exist)."""
    token = (ui_token or "").strip()
    return token or os.environ.get("IBM_QUANTUM_TOKEN", "").strip() or None


def resolve_ibm_instance(ui_instance: str | None) -> str | None:
    """Instance CRN precedence: explicit UI input, then IBM_QUANTUM_INSTANCE."""
    instance = (ui_instance or "").strip()
    return instance or os.environ.get("IBM_QUANTUM_INSTANCE", "").strip() or None


def build_local_engine(shots: int, seed: int) -> QuantumEngine:
    """Local Aer simulator engine — used by default and as the IBM fallback."""
    from qiskit.transpiler import generate_preset_pass_manager
    from qiskit_aer import AerSimulator
    from qiskit_aer.primitives import SamplerV2 as AerSamplerV2

    backend = AerSimulator()
    return QuantumEngine(
        kind="local",
        label="local Aer simulator",
        sampler=AerSamplerV2(default_shots=shots, seed=seed),
        pass_manager=generate_preset_pass_manager(
            optimization_level=1, backend=backend
        ),
        max_qubits=24,  # statevector memory limit on a typical machine
    )


def _make_service(token: str | None, instance: str | None):
    """Create a QiskitRuntimeService from an explicit token, or — if none is
    given — from credentials previously stored with
    ``QiskitRuntimeService.save_account(...)``. Raises ValueError with a
    user-presentable message when no usable credentials exist."""
    try:
        from qiskit_ibm_runtime import QiskitRuntimeService
    except ImportError as exc:  # pragma: no cover - pinned in requirements
        raise ValueError(f"qiskit-ibm-runtime is not installed: {exc}") from exc

    try:
        if token:
            # channel "ibm_quantum_platform" is the current (2025+) platform.
            return QiskitRuntimeService(
                channel="ibm_quantum_platform", token=token, instance=instance
            )
        # No explicit token: fall back to a saved account (~/.qiskit/qiskit-ibm.json).
        return QiskitRuntimeService(instance=instance) if instance \
            else QiskitRuntimeService()
    except Exception as exc:
        raise ValueError(
            "No IBM Quantum credentials found. Paste your API key (and, ideally, "
            "your instance CRN) in the sidebar, set the IBM_QUANTUM_TOKEN / "
            "IBM_QUANTUM_INSTANCE environment variables, or run "
            "QiskitRuntimeService.save_account(...) once. Create a key at "
            f"https://quantum.cloud.ibm.com. (Details: {exc})"
        ) from exc


def ibm_connection_report(token: str | None, instance: str | None) -> list[dict]:
    """Connect and list the operational backends visible to the account — used
    by the sidebar's "Test connection" button so users can confirm their setup
    *without* submitting a (quota-consuming) job. Raises ValueError on failure."""
    service = _make_service(token, instance)
    report: list[dict] = []
    try:
        backends = service.backends(operational=True)
    except Exception as exc:
        raise ValueError(f"Connected, but could not list backends: {exc}") from exc
    for backend in backends:
        try:
            pending = backend.status().pending_jobs
        except Exception:
            pending = None
        report.append(
            {
                "name": backend.name,
                "qubits": getattr(backend, "num_qubits", None),
                "simulator": bool(getattr(backend, "simulator", False)),
                "pending_jobs": pending,
            }
        )
    if not report:
        raise ValueError(
            "Connected, but no operational backends are available to this "
            "instance. Check that your instance has access to a quantum system."
        )
    return report


def build_ibm_engine(
    token: str | None,
    instance: str | None,
    backend_name: str | None,
    shots: int,
    min_qubits: int,
) -> QuantumEngine:
    """Real IBM Quantum hardware via the cloud API.

    Raises ValueError with a user-presentable message on any setup problem
    (missing credentials, bad key, no matching backend) so the UI can decide
    whether to fall back to the local simulator.
    """
    from qiskit.transpiler import generate_preset_pass_manager
    from qiskit_ibm_runtime import SamplerV2 as RuntimeSamplerV2

    service = _make_service(token, instance)  # raises ValueError on bad creds
    try:
        if backend_name and backend_name.strip():
            backend = service.backend(backend_name.strip())
        else:
            backend = service.least_busy(
                operational=True, simulator=False, min_num_qubits=min_qubits
            )
    except Exception as exc:
        raise ValueError(
            f"Connected to IBM Quantum, but could not select a backend: {exc}. "
            "If you named a specific backend, check the spelling; otherwise your "
            "instance may have no device with enough qubits."
        ) from exc

    sampler = RuntimeSamplerV2(mode=backend)
    sampler.options.default_shots = shots
    return QuantumEngine(
        kind="ibm",
        label=f"IBM Quantum — {backend.name} ({backend.num_qubits} qubits)",
        sampler=sampler,
        # Level 3 squeezes circuit depth as hard as possible — worth it on
        # noisy hardware even though transpilation takes longer.
        pass_manager=generate_preset_pass_manager(
            optimization_level=3, backend=backend
        ),
        max_qubits=int(backend.num_qubits),
    )
