# This is the baseimage used by masa project
# All common installations should be done in this image
FROM python:3

WORKDIR /usr/src/app

COPY . .

RUN pip install -r requirements.txt

#Install Cron
RUN apt-get update
RUN apt-get -y install cron