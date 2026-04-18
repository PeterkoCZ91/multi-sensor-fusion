FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fusion_service.py .

CMD ["python", "-u", "fusion_service.py", "/app/config.yaml"]
