FROM python:3.12-slim
WORKDIR /app
RUN apt-get install -y --no-install-recommends libnonexistent-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER 10001
EXPOSE 5000
CMD ["python", "app.py"]
