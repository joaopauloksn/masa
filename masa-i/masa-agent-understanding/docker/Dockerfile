FROM jpksn/masa-baseimage:0.0.1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/

CMD ["python", "task.py"]
