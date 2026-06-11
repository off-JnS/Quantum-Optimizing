@echo off
REM Quick start script for Quantum Portfolio Optimizer on Windows

echo.
echo ============================================
echo  Quantum Portfolio Optimizer - Local Start
echo ============================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.10+ from python.org
    pause
    exit /b 1
)

REM Check if requirements are installed
python -c "import streamlit; import qiskit; import pandas" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies
        pause
        exit /b 1
    )
)

echo.
echo Starting Quantum Portfolio Optimizer...
echo.
echo Once the app opens, you can access it at:
echo   - Local: http://localhost:8502
echo   - Network: http://192.168.178.101:8502
echo.
echo Press Ctrl+C in this window to stop the app.
echo.

REM Start Streamlit
streamlit run app.py

pause
