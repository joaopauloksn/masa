# This is the baseimage used by masa project
# All common installations should be done in this image
# Pin to 3.12-slim: python:3 resolves to 3.14 which has no TensorFlow wheels.
FROM python:3.12-slim

WORKDIR /usr/src/app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt