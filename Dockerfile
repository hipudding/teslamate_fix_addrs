FROM ubuntu:22.04

RUN mkdir /root/app

RUN apt update && apt install -y python3 python3-pip

COPY * /root/app

RUN pip install -r /root/app/requirements.txt

ENTRYPOINT ["python3", "/root/app/teslamate_fix_addrs.py"]
