FROM jpksn/masa-baseimage:1.0.1

# Copy only requirements.txt first to leverage caching
COPY requirements.txt .

# Install dependencies
RUN pip install -r requirements.txt

# Copy the rest of the code
COPY . .

CMD ["python3","-u","src/task.py"]
