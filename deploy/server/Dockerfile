FROM mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements-server.txt /app/requirements-server.txt
RUN python -m pip install \
    --no-cache-dir \
    --index-url https://mirrors.aliyun.com/pypi/simple/ \
    -r /app/requirements-server.txt

COPY erp_automation /app/erp_automation
COPY lingxing_automation /app/lingxing_automation
COPY shipment_automation /app/shipment_automation
COPY CLIENT_VERSION /app/CLIENT_VERSION
COPY data/china_workdays.json /app/data/china_workdays.json
COPY rules /app/rules

ENTRYPOINT ["python", "-m", "erp_automation.coordination.server_main"]
