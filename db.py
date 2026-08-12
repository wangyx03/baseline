import os

import mysql.connector


def get_db():
    db = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
    )

    cursor = db.cursor()
    cursor.execute("SET time_zone = '+00:00'")
    cursor.close()

    return db
