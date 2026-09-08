FROM ubuntu:22.04

RUN mkdir /root/app

RUN apt update && DEBIAN_FRONTEND=noninteractive apt install -y python3 python3-pip tzdata

COPY * /root/app

RUN pip install -r /root/app/requirements.txt

ENTRYPOINT ["python3", "/root/app/teslamate_fix_addrs.py"]
