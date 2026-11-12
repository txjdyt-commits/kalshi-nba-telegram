FROM python:3.12-slim
WORKDIR /app
COPY bot.py telegram_transport.py ./
ENV PYTHONUNBUFFERED=1 STATE_DIR=/data
VOLUME ["/data"]
CMD ["python", "-u", "bot.py"]
