from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "Flask service is running"

@app.route("/health")
def health():
    return {"status": "ok"}

@app.route("/new_token")
def new_token():
    return {"status": "failed"}
