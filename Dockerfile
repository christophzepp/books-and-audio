FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY admin_static/ ./admin_static/
# Empty named volumes inherit these owners on first use.
RUN install -d -m 0700 -o 1000 -g 1000 /installation /keys /state /state/control /admin-state /admin-state/control
USER 1000:1000
CMD ["python", "admin_web.py"]
