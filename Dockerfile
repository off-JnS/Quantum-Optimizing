# Quantum Portfolio Optimizer — production image
# Build:  docker build -t quantum-portfolio .
# Run:    docker run -p 8501:8501 -e IBM_QUANTUM_TOKEN=... quantum-portfolio
FROM python:3.12-slim

# Faster, smaller, reproducible installs
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so code changes don't bust the pip layer cache
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8501

# Streamlit's built-in health endpoint keeps orchestrators honest
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4).status == 200 else 1)"

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
