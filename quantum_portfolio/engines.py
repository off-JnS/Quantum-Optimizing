"""Quantum execution engines.

Two interchangeable backends drive the QAOA subproblems:

* ``build_local_engine``  — Qiskit Aer simulator on this machine (free, instant,
  no account needed).
* ``build_ibm_engine``    — real IBM Quantum hardware through the cloud API
  (qiskit-ibm-runtime). Needs an API key from https://quantum.cloud.ibm.com.

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
    """Token precedence: explicit UI input, then the IBM_QUANTUM_TOKEN env var
    (the way a server deployment should supply it)."""
    token = (ui_token or "").strip()
    return token or os.environ.get("IBM_QUANTUM_TOKEN", "").strip() or None


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


def build_ibm_engine(
    token: str | None,
    instance: str | None,
    backend_name: str | None,
    shots: int,
    min_qubits: int,
) -> QuantumEngine:
    """Real IBM Quantum hardware via the cloud API.

    Raises ValueError with a user-presentable message on any setup problem
    (missing token, bad credentials, no matching backend) so the UI can decide
    whether to fall back to the local simulator.
    """
    if not token:
        raise ValueError(
            "No IBM Quantum API key found. Paste one in the sidebar or set the "
            "IBM_QUANTUM_TOKEN environment variable "
            "(create a key at https://quantum.cloud.ibm.com)."
        )
    try:
        from qiskit.transpiler import generate_preset_pass_manager
        from qiskit_ibm_runtime import QiskitRuntimeService
        from qiskit_ibm_runtime import SamplerV2 as RuntimeSamplerV2
    except ImportError as exc:  # pragma: no cover - pinned in requirements
        raise ValueError(f"qiskit-ibm-runtime is not installed: {exc}") from exc

    try:
        service = QiskitRuntimeService(
            channel="ibm_quantum_platform",
            token=token,
            instance=(instance or "").strip() or None,
        )
        if backend_name and backend_name.strip():
            backend = service.backend(backend_name.strip())
        else:
            backend = service.least_busy(
                operational=True, simulator=False, min_num_qubits=min_qubits
            )
    except Exception as exc:
        raise ValueError(
            f"Could not connect to IBM Quantum: {exc}. Check your API key "
            "(and instance CRN, if your account needs one)."
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
