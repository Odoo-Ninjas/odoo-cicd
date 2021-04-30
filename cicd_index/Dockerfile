FROM ubuntu:20.04
ENV FLASK_RUN_HOST=0.0.0.0
RUN apt update && apt install -y curl apt-transport-https ca-certificates curl gnupg2 software-properties-common
RUN curl -fsSL https://download.docker.com/linux/ubuntu/gpg | apt-key add -
RUN add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu focal stable"
RUN apt update && DEBIAN_FRONTEND="noninteractive" apt install -y python3-pip  docker-ce
RUN mkdir -p /root/.ssh/socket
ADD index_ident /root/.ssh/id_rsa
RUN chmod 400 /root/.ssh/id_rsa
RUN chmod 500 /root/.ssh

WORKDIR /root
COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt
EXPOSE 5000
ADD ssh_config /root/.ssh/config
CMD ["flask", "run"]
WORKDIR /

RUN apt install -y rsync