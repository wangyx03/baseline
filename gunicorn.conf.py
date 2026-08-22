import logging


# =========================================================
# Gunicorn Basic Settings
# =========================================================

bind = "127.0.0.1:8001"

workers = 2

worker_class = "sync"

timeout = 120

graceful_timeout = 30

keepalive = 5


# =========================================================
# Worker Recycling
#
# 防止长期运行后某些请求或第三方库造成内存缓慢增长。
# 每个 worker 处理一定数量请求后自动重启。
# =========================================================

max_requests = 5000

max_requests_jitter = 500


# =========================================================
# Logging
# =========================================================

accesslog = "-"

errorlog = "-"

capture_output = True

loglevel = "info"


# =========================================================
# Access Log Filter
#
# Recording 页面每 2 秒请求一次：
#
#     /api/recordings/version
#
# 这个请求用于多客户端同步，但没有必要写入 access log。
# 其他 GET / POST / PUT / DELETE 仍正常记录。
# =========================================================

class IgnoreNoisyEndpointsFilter(logging.Filter):

    def filter(self, record):

        message = record.getMessage()

        ignored_paths = (
            "/api/recordings/version",
        )

        for path in ignored_paths:

            if path in message:
                return False

        return True


def post_fork(server, worker):

    access_logger = logging.getLogger(
        "gunicorn.access"
    )

    # 避免因为 worker reload 重复添加相同 filter
    for existing_filter in access_logger.filters:

        if isinstance(
            existing_filter,
            IgnoreNoisyEndpointsFilter
        ):
            return

    access_logger.addFilter(
        IgnoreNoisyEndpointsFilter()
    )