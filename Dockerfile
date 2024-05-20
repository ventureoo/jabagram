FROM python:3-alpine

WORKDIR /app

COPY requirements.txt ./
RUN apk update && apk upgrade
RUN apk add --update --no-cache build-base gcc python3-dev musl-dev libffi-dev openssl-dev sqlite
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./

ENTRYPOINT [ "python", "./jabagram.py" ]
