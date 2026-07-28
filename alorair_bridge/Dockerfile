FROM python:3.12-slim
RUN pip install --no-cache-dir paho-mqtt==2.1.0
COPY alorair_cloud.py /app/alorair_cloud.py
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD [ "/run.sh" ]
