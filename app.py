from pathlib import Path
import os

from dotenv import load_dotenv
from flask import Flask


# ==========================================
# 基础配置
# ==========================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(
    BASE_DIR / ".env"
)


# ==========================================
# 功能模块开关
# ==========================================

# Recording 模块
ENABLE_RECORDING = True

# Staff Schedule 模块
ENABLE_STAFFSCHEDULE = True


# ==========================================
# 创建 Flask App
# ==========================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY"
)

app.config["SESSION_PERMANENT"] = False


# ==========================================
# Recording
# ==========================================

if ENABLE_RECORDING:

    from recording.auth import (
        auth_bp,
        init_auth
    )

    from recording.recording import (
        recording_bp
    )

    from recording.weekly_inventory import (
        weekly_bp
    )

    # 初始化登录
    init_auth(app)

    # 注册 Blueprint
    app.register_blueprint(
        auth_bp
    )

    app.register_blueprint(
        recording_bp
    )

    app.register_blueprint(
        weekly_bp
    )


# ==========================================
# Staff Schedule
# ==========================================

if ENABLE_STAFFSCHEDULE:

    from staffschedule.availability import (
        availability_bp
    )

    app.register_blueprint(
        availability_bp
    )


# ==========================================
# 本地开发启动
# ==========================================

if __name__ == "__main__":

    app.run(
        debug=True
    )