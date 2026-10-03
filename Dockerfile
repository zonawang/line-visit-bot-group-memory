FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py event_store.py semantic_selector.py faq.json ./
RUN useradd --system --uid 10001 bot
USER 10001

ENV PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
