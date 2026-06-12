"""Quantum Portfolio Optimizer — core package.

Modules:
    data       — ticker parsing, price download/validation, annualized statistics
    engines    — quantum execution engines (local Aer simulator / IBM Quantum hardware)
    optimizer  — screening, hierarchical QAOA selection, classical fallbacks, weighting
"""

TRADING_DAYS = 252      # annualization factor for daily returns
MIN_HISTORY_DAYS = 60   # minimum usable trading days per ticker
MAX_TICKERS = 500       # upper bound on the input universe
