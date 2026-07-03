import logging
import sys
import os
import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

os.makedirs("logs", exist_ok=True)

logger = logging.getLogger("app")
logger.setLevel(logging.INFO)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(fmt)
logger.addHandler(sh)

fh = logging.FileHandler("logs/app.log")
fh.setFormatter(fmt)
logger.addHandler(fh)



app = FastAPI(title="Docker Project API")


def get_conn():
    try:
        return psycopg2.connect(
            host=os.getenv("DB_HOST", "db"),
            dbname=os.getenv("POSTGRES_DB"),
            user=os.getenv("POSTGRES_USER"),
            password=os.getenv("POSTGRES_PASSWORD"),
        )
    except Exception as e:
        logger.error(f"DB connection error: {e}")
        return None


def init_db():
    conn = get_conn()
    if conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id   SERIAL PRIMARY KEY,
                    name VARCHAR(200) NOT NULL
                )
            """)
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")


init_db()


class Item(BaseModel):
    name: str


@app.get("/")
def root():
    logger.info("GET / called")
    return {"message": "welcome to my shop", "status": "running"}


@app.post("/items/")
def create_item(item: Item):
    conn = get_conn()
    if not conn:
        raise HTTPException(status_code=500, detail="Database unavailable")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO items (name) VALUES (%s) RETURNING id", (item.name,))
        item_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    logger.info(f"POST /items/ -> created id={item_id} name={item.name}")
    return {"id": item_id, "name": item.name}


@app.get("/items/")
def list_items():
    conn = get_conn()
    if not conn:
        raise HTTPException(status_code=500, detail="Database unavailable")
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM items ORDER BY id")
        rows = cur.fetchall()
    conn.close()
    items = [{"id": r[0], "name": r[1]} for r in rows]
    logger.info(f"GET /items/ -> returned {len(items)} items")
    return {"items": items}


@app.get("/items/search")
def search_items(name: str):
    conn = get_conn()
    if not conn:
        raise HTTPException(status_code=500, detail="Database unavailable")
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM items WHERE name ILIKE %s ORDER BY id", (f"%{name}%",))
        rows = cur.fetchall()
    conn.close()
    items = [{"id": r[0], "name": r[1]} for r in rows]
    logger.info(f"GET /items/search?name={name} -> returned {len(items)} items")
    return {"items": items, "count": len(items)}


@app.delete("/items/{item_id}")
def delete_item(item_id: int):
    conn = get_conn()
    if not conn:
        raise HTTPException(status_code=500, detail="Database unavailable")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM items WHERE id = %s", (item_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Item not found")
        cur.execute("DELETE FROM items WHERE id = %s", (item_id,))
    conn.commit()
    conn.close()
    logger.info(f"DELETE /items/{item_id} -> deleted")
    return {"message": "Item deleted", "id": item_id}


@app.get("/health")
def health():
    logger.info("GET /health called")
    return {"status": "healthy"}
