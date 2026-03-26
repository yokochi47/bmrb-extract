from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "Flask service is running"

@app.route("/health")
def health():
    return {"status": "ok"}

