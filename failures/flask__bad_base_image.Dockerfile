FROM python:3.99-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER 10001
EXPOSE 5000
CMD ["python", "app.py"]
