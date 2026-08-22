import logging


class IgnoreVersionEndpointFilter(logging.Filter):

    def filter(self, record):

        message = record.getMessage()

        if "/api/recordings/version" in message:
            return False

        return True


def post_fork(server, worker):

    access_logger = logging.getLogger("gunicorn.access")

    access_logger.addFilter(
        IgnoreVersionEndpointFilter()
    )