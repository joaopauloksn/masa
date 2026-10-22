FROM jpksn/masa-baseimage:1.0.1

# Copy requirements first to leverage layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3","-u","task.py"]
