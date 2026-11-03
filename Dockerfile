FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 10001 -m vault && mkdir /data && chown vault:vault /data
COPY --chown=vault:vault . .
USER vault
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "app:create_app()"]
