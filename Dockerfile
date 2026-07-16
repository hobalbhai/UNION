FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt update && apt install -y python3 python3-pip openjdk-11-jdk unzip wget git aapt zipalign && \
    rm -rf /var/lib/apt/lists/*

# apktool (GitHub release)
RUN wget -O /usr/local/bin/apktool https://raw.githubusercontent.com/iBotPeaches/Apktool/master/scripts/linux/apktool && \
    chmod +x /usr/local/bin/apktool && \
    wget -O /usr/local/bin/apktool.jar https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar && \
    chmod +x /usr/local/bin/apktool.jar
ENV APKTOOL_JAR=/usr/local/bin/apktool.jar

# apksigner – সরাসরি build-tools থেকে
RUN mkdir -p /opt/build-tools && cd /opt/build-tools && \
    wget -q https://dl.google.com/android/repository/build-tools_r34-linux.zip && \
    unzip build-tools_r34-linux.zip && \
    rm build-tools_r34-linux.zip
ENV PATH=$PATH:/opt/build-tools/android-14

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY . .

CMD ["python3", "bot.py"]
