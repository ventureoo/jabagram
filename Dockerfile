FROM python:3

WORKDIR /app

COPY requirements.txt ./
RUN apt update -y && apt upgrade -y
RUN apt install -y sqlite3
RUN pip install --no-cache-dir -r requirements.txt
RUN pip3 install --upgrade slixmpp
RUN pip3 install --upgrade aiohttp
COPY . ./

ENTRYPOINT [ "python", "./jabagram.py" ]
