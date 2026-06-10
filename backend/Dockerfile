FROM python:3.12-alpine

ENV PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

RUN apk add --no-cache gcc musl-dev libffi-dev git

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]

