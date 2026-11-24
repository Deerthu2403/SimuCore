FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 monitor
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor ./monitor
USER monitor
EXPOSE 8200
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s CMD \
  ["python", "-c", "import urllib.request as u;u.urlopen('http://127.0.0.1:8200/health/ready',timeout=2)"]
CMD ["uvicorn", "monitor.main:app", "--host", "0.0.0.0", "--port", "8200", "--no-access-log"]
