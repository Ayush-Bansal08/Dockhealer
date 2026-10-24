FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER 10001
EXPOSE 5000
CMD ["python", "-m", "flask", "--app", "app", "run", "--port", "5000"]
