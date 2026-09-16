FROM python:3.11-slim

# Without this Python block-buffers stdout when it is a pipe, so nothing the app
# prints ever reaches `heroku logs` and a broken start looks like total silence.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .

# ffmpeg is used for thumbnails and video duration and stays in the image.
# gcc/python3-dev are insurance in case a dependency has no prebuilt wheel for
# this platform; they are purged in the same layer so they cost no image size.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg gcc python3-dev \
 && pip3 install --no-cache-dir -U pip setuptools wheel \
 && pip3 install --no-cache-dir -U -r requirements.txt \
 && apt-get purge -y --auto-remove gcc python3-dev \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /root/.cache

COPY . .

# Heroku ignores EXPOSE and injects $PORT instead; main.py binds whatever it gets.
EXPOSE 8080

CMD ["python3", "main.py"]
