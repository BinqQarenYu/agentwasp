FROM python:3.12-slim

RUN groupadd -g 1000 agent && useradd -u 1000 -g agent -m agent

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

RUN chown -R agent:agent /app

USER agent

CMD ["python", "-m", "src.main"]
