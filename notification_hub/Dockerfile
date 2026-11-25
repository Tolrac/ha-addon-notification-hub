FROM python:3.13-alpine
RUN pip install --no-cache-dir aiohttp
COPY app /app
CMD ["python3", "-u", "/app/server.py"]
