import os
from threading import Lock

from mysql.connector import pooling


_db_pool = None
_db_pool_lock = Lock()


def _get_pool():

    global _db_pool

    if _db_pool is None:

        with _db_pool_lock:

            if _db_pool is None:

                _db_pool = pooling.MySQLConnectionPool(
                    pool_name="baseline_pool",
                    pool_size=int(
                        os.getenv(
                            "DB_POOL_SIZE",
                            "5"
                        )
                    ),
                    pool_reset_session=True,
                    host=os.getenv("DB_HOST"),
                    port=int(
                        os.getenv(
                            "DB_PORT",
                            "3306"
                        )
                    ),
                    user=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    database=os.getenv("DB_NAME"),
                )

    return _db_pool


def get_db():

    db = _get_pool().get_connection()

    cursor = db.cursor()

    try:

        cursor.execute(
            "SET time_zone = '+00:00'"
        )

    finally:

        cursor.close()

    return db
