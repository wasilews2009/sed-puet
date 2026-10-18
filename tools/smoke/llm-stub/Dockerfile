FROM puet/sed-app:0.1.0
COPY stub.py /opt/stub/stub.py
WORKDIR /opt/stub
CMD ["uvicorn", "stub:app", "--host", "0.0.0.0", "--port", "8000"]
