from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv
import mysql.connector
import os
import csv
import io
from pathlib import Path


app = Flask(__name__)


# =========================================================
# 读取项目根目录 baseline/.env
# =========================================================

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def get_db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


# =========================================================
# Recording 页面
# =========================================================

@app.route("/recording/<int:store_id>")
def recording(store_id):

    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute(
            """
            SELECT store_name
            FROM stores
            WHERE store_id = %s
            """,
            (store_id,)
        )

        store = cursor.fetchone()

        if store is None:
            return "Store not found", 404

        return render_template(
            "recording.html",
            store_id=store_id,
            store_name=store["store_name"]
        )

    finally:
        cursor.close()
        db.close()


# =========================================================
# 新增一条 Recording
# =========================================================

@app.route("/api/record", methods=["POST"])
def record():

    data = request.get_json() or {}

    sku = str(data.get("sku", "")).strip()
    live_id = str(data.get("live_id", "")).strip()
    store_id = data.get("store_id")


    if not live_id:
        return jsonify({
            "success": False,
            "message": "LIVE ID is required"
        }), 400


    if not sku:
        return jsonify({
            "success": False,
            "message": "SKU is empty"
        }), 400


    if store_id is None:
        return jsonify({
            "success": False,
            "message": "Store ID is required"
        }), 400


    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        # =================================================
        # 1. 如果 book_sku 不存在这个 ISBN，就自动新增
        #
        # isbn 是 PRIMARY KEY
        # 已存在时 INSERT IGNORE 不会报错，也不会重复插入
        # =================================================

        cursor.execute(
            """
            INSERT IGNORE INTO book_sku (isbn)
            VALUES (%s)
            """,
            (sku,)
        )


        # =================================================
        # 2. 写入 sku_recording
        # quantity 使用数据库默认值 1
        # =================================================

        cursor.execute(
            """
            INSERT INTO sku_recording (
                sku,
                store_id,
                live_id
            )
            VALUES (%s, %s, %s)
            """,
            (
                sku,
                store_id,
                live_id
            )
        )

        recording_id = cursor.lastrowid


        # =================================================
        # 3. 查询书籍信息
        # =================================================

        cursor.execute(
            """
            SELECT
                book_title,
                book_author,
                book_format
            FROM book_sku
            WHERE isbn = %s
            """,
            (sku,)
        )

        book = cursor.fetchone()


        db.commit()


        return jsonify({
            "success": True,
            "recording_id": recording_id,
            "sku": sku,
            "live_id": live_id,
            "book": book
        })


    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


    finally:

        cursor.close()
        db.close()


# =========================================================
# 获取当前 Store + LIVE ID 的 Recording
# =========================================================

@app.route("/api/recordings", methods=["GET"])
def get_recordings():

    store_id = request.args.get(
        "store_id",
        type=int
    )

    live_id = str(
        request.args.get(
            "live_id",
            ""
        )
    ).strip()


    if store_id is None:
        return jsonify({
            "success": False,
            "message": "Store ID is required"
        }), 400


    if not live_id:
        return jsonify({
            "success": False,
            "message": "LIVE ID is required"
        }), 400


    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        cursor.execute(
            """
            SELECT
                sr.recording_id,
                sr.sku,
                bs.book_title,
                sr.recorded_at

            FROM sku_recording sr

            LEFT JOIN book_sku bs
                ON sr.sku = bs.isbn

            WHERE sr.store_id = %s
              AND sr.live_id = %s

            ORDER BY sr.recording_id DESC
            """,
            (
                store_id,
                live_id
            )
        )

        rows = cursor.fetchall()


        # datetime 转成普通字符串，避免浏览器显示 GMT
        for row in rows:

            if row["recorded_at"]:

                row["recorded_at"] = (
                    row["recorded_at"]
                    .strftime("%Y-%m-%d %H:%M:%S")
                )


        return jsonify(rows)


    except Exception as e:

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


    finally:

        cursor.close()
        db.close()


# =========================================================
# 修改 Recording
# 目前只修改 SKU
# =========================================================

@app.route(
    "/api/recordings/<int:recording_id>",
    methods=["PUT"]
)
def update_recording(recording_id):

    data = request.get_json() or {}

    sku = str(
        data.get(
            "sku",
            ""
        )
    ).strip()

    live_id = str(
        data.get(
            "live_id",
            ""
        )
    ).strip()

    store_id = data.get("store_id")


    if not live_id:
        return jsonify({
            "success": False,
            "message": "LIVE ID is required"
        }), 400


    if not sku:
        return jsonify({
            "success": False,
            "message": "SKU is empty"
        }), 400


    if store_id is None:
        return jsonify({
            "success": False,
            "message": "Store ID is required"
        }), 400


    db = get_db()
    cursor = db.cursor()

    try:

        # 如果编辑成了一个新的 ISBN，
        # 同样自动加入 book_sku
        cursor.execute(
            """
            INSERT IGNORE INTO book_sku (isbn)
            VALUES (%s)
            """,
            (sku,)
        )


        cursor.execute(
            """
            UPDATE sku_recording

            SET sku = %s

            WHERE recording_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                sku,
                recording_id,
                store_id,
                live_id
            )
        )


        if cursor.rowcount == 0:

            db.rollback()

            return jsonify({
                "success": False,
                "message":
                    "Recording not found in current LIVE"
            }), 404


        db.commit()


        return jsonify({
            "success": True
        })


    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


    finally:

        cursor.close()
        db.close()


# =========================================================
# 删除 Recording
# =========================================================

@app.route(
    "/api/recordings/<int:recording_id>",
    methods=["DELETE"]
)
def delete_recording(recording_id):

    data = request.get_json() or {}

    live_id = str(
        data.get(
            "live_id",
            ""
        )
    ).strip()

    store_id = data.get("store_id")


    if not live_id:
        return jsonify({
            "success": False,
            "message": "LIVE ID is required"
        }), 400


    if store_id is None:
        return jsonify({
            "success": False,
            "message": "Store ID is required"
        }), 400


    db = get_db()
    cursor = db.cursor()

    try:

        cursor.execute(
            """
            DELETE FROM sku_recording

            WHERE recording_id = %s
              AND store_id = %s
              AND live_id = %s
            """,
            (
                recording_id,
                store_id,
                live_id
            )
        )


        if cursor.rowcount == 0:

            db.rollback()

            return jsonify({
                "success": False,
                "message":
                    "Recording not found in current LIVE"
            }), 404


        db.commit()


        return jsonify({
            "success": True
        })


    except Exception as e:

        db.rollback()

        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


    finally:

        cursor.close()
        db.close()


# =========================================================
# 下载当前 LIVE 的 CSV
# =========================================================

@app.route(
    "/api/recordings/download",
    methods=["GET"]
)
def download_recordings():

    store_id = request.args.get(
        "store_id",
        type=int
    )

    live_id = str(
        request.args.get(
            "live_id",
            ""
        )
    ).strip()


    if store_id is None:
        return "Store ID is required", 400


    if not live_id:
        return "LIVE ID is required", 400


    db = get_db()
    cursor = db.cursor(dictionary=True)

    try:

        # 店铺名称
        cursor.execute(
            """
            SELECT store_name
            FROM stores
            WHERE store_id = %s
            """,
            (store_id,)
        )

        store = cursor.fetchone()

        if store is None:
            return "Store not found", 404


        # 当前 LIVE 数据
        cursor.execute(
            """
            SELECT
                sr.sku,
                bs.book_title,
                sr.recorded_at

            FROM sku_recording sr

            LEFT JOIN book_sku bs
                ON sr.sku = bs.isbn

            WHERE sr.store_id = %s
              AND sr.live_id = %s

            ORDER BY sr.recording_id ASC
            """,
            (
                store_id,
                live_id
            )
        )

        rows = cursor.fetchall()


        output = io.StringIO()

        writer = csv.writer(output)


        writer.writerow([
            "No",
            "SKU",
            "Book Title",
            "Recorded Time ET"
        ])


        for index, row in enumerate(
            rows,
            start=1
        ):

            recorded_at = ""

            if row["recorded_at"]:

                recorded_at = (
                    row["recorded_at"]
                    .strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    +
                    " ET"
                )


            writer.writerow([
                index,
                row["sku"],
                row["book_title"] or "",
                recorded_at
            ])


        # UTF-8 BOM，Excel 打开更稳定
        csv_content = (
            "\ufeff"
            +
            output.getvalue()
        )


        store_name = (
            store["store_name"]
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
        )


        safe_live_id = (
            live_id
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
        )


        filename = (
            f"{store_name}_"
            f"{safe_live_id}_"
            f"recording.csv"
        )


        return Response(
            csv_content,
            mimetype=
                "text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{filename}"'
            }
        )


    finally:

        cursor.close()
        db.close()


# =========================================================
# 启动 Flask
# =========================================================

if __name__ == "__main__":
    app.run(debug=True)