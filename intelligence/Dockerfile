FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OMP_NUM_THREADS=2
WORKDIR /app
RUN useradd --create-home --uid 10001 ai
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY simucore_ai ./simucore_ai
COPY models ./models
COPY results ./results
USER ai
EXPOSE 8300
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s CMD \
  ["python", "-c", "import urllib.request as u;u.urlopen('http://127.0.0.1:8300/health/ready',timeout=2)"]
CMD ["uvicorn", "simucore_ai.main:app", "--host", "0.0.0.0", "--port", "8300", "--no-access-log"]
