from pathlib import Path
import os

from dotenv import load_dotenv
from flask import Flask

from recording.auth import auth_bp, init_auth
from recording.recording import recording_bp
from recording.weekly_inventory import weekly_bp
from staffschedule.availability import availability_bp


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(
    BASE_DIR / ".env"
)


app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY"
)

app.config["SESSION_PERMANENT"] = False


# 初始化登录
init_auth(app)


# 注册功能模块
app.register_blueprint(auth_bp)

app.register_blueprint(recording_bp)

app.register_blueprint(weekly_bp)

app.register_blueprint(availability_bp)


if __name__ == "__main__":

    app.run(
        debug=True
    )