# One recipe, eight images: each image contains the shared library and exactly one service.
#   docker build --build-arg SERVICE=account -t simucore/bank-account .
FROM python:3.12-slim
ARG SERVICE
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BANK_BIND=0.0.0.0 \
    BANK_DATA_DIR=/data BANK_SERVICE=${SERVICE}
RUN test -n "${SERVICE}"
WORKDIR /app
RUN useradd --create-home --uid 10001 bank && mkdir /data && chown bank:bank /data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dependency_map.json .
COPY bankcommon ./bankcommon
COPY services/__init__.py ./services/__init__.py
COPY services/${SERVICE} ./services/${SERVICE}
USER bank
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 CMD \
  ["python", "-c", "import os,json,urllib.request as u;s=json.load(open('dependency_map.json'))['services'][os.environ['BANK_SERVICE']];u.urlopen(f\"http://127.0.0.1:{s['port']}/health/live\",timeout=2)"]
STOPSIGNAL SIGTERM
CMD ["sh", "-c", "exec python -m bankcommon.run \"$BANK_SERVICE\""]
