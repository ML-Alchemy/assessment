FROM python:3.11-slim
WORKDIR /scripts
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
