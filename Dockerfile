FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt update && apt install -y python3 python3-pip openjdk-11-jdk unzip wget
RUN wget -O /usr/local/bin/apktool https://raw.githubusercontent.com/iBotPeaches/Apktool/master/scripts/linux/apktool && chmod +x /usr/local/bin/apktool
RUN wget -O /usr/local/bin/apktool.jar https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.9.3.jar && chmod +x /usr/local/bin/apktool.jar
ENV APKTOOL_JAR=/usr/local/bin/apktool.jar
COPY requirements.txt .
RUN pip3 install -r requirements.txt
WORKDIR /app
COPY bot.py .
CMD ["python3", "bot.py"]
