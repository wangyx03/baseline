from pathlib import Path
import os

from dotenv import load_dotenv
from flask import Flask, render_template
from flask_login import login_required

from auth.auth import auth_bp, init_auth
from recording.recording import recording_bp
from recording.weekly_inventory import weekly_bp
from staffschedule.availability import availability_bp
from staffschedule.availability_management import availability_management_bp
from staffschedule.schedule import schedule_bp


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(
    BASE_DIR / ".env"
)


app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY"
)

app.config["SESSION_PERMANENT"] = False


# =========================
# 初始化登录
# =========================

init_auth(app)


# =========================
# 注册功能模块
# =========================

app.register_blueprint(auth_bp)

app.register_blueprint(recording_bp)

app.register_blueprint(weekly_bp)

app.register_blueprint(availability_bp)

app.register_blueprint(availability_management_bp)

app.register_blueprint(schedule_bp)

# =========================
# 系统首页
# =========================

@app.route("/")
@login_required
def index():

    return render_template(
        "index.html",
        show_dashboard=False
    )


# =========================
# 启动
# =========================

if __name__ == "__main__":

    app.run(
        debug=True
    )