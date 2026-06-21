FROM python:3.10-slim

WORKDIR /app

# Εγκατάσταση βιβλιοθηκών συστήματος για τη MariaDB
RUN apt-get update && apt-get install -y \
    gcc \
    libmariadb-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ARG CACHE_BUST=1
COPY . .

EXPOSE 5411

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5411"]
