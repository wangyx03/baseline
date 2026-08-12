from flask import Flask
from dotenv import load_dotenv
import os
from pathlib import Path

from auth import auth_bp, init_auth
from recording import recording_bp
from weekly_inventory import weekly_bp


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
app.config["SESSION_PERMANENT"] = False


init_auth(app)
app.register_blueprint(auth_bp)
app.register_blueprint(recording_bp)
app.register_blueprint(weekly_bp)


if __name__ == "__main__":
    app.run(debug=True)
