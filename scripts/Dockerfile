FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --system --uid 10001 relay
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
USER 10001:10001
CMD ["python", "status.py"]
