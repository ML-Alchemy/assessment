FROM python:latest
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY main.py .
ENV ADMIN_USERNAME=admin
ENV ADMIN_PASSWORD=relay-admin-2026
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
