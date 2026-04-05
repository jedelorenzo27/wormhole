FROM python:3.12-slim

LABEL maintainer="wormhole"
LABEL description="Fix broken auto-play on Roku streaming apps"

WORKDIR /app

COPY wormhole.py .

ENTRYPOINT ["python", "-u", "wormhole.py"]
CMD ["run"]
