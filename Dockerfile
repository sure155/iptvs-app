FROM python:3.11-alpine

WORKDIR /app

RUN pip install --no-cache-dir

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "-u", "iptv_proxy.py"]
