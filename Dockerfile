FROM python:3

WORKDIR /app

COPY requirements.txt ./
RUN apt update -y && apt upgrade -y
RUN apt install -y sqlite3
RUN pip install --upgrade --no-cache-dir -r requirements.txt
COPY . ./

ENTRYPOINT [ "python", "./jabagram.py" ]
