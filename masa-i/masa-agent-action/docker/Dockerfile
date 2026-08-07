FROM jpksn/masa-baseimage:0.0.1

COPY . .

RUN pip install -r requirements.txt

USER masa

CMD ["python3","-u","src/task.py"]
