import json

from dotenv import load_dotenv
from sqlalchemy import text
from weaviateconnect import get_weaviate_client, get_weaviate_collection
from postgreconnect import engine


def _normalize_image_urls(value):
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if isinstance(value, list):
        return [str(url) for url in value if url]
    try:
        return [str(url) for url in list(value) if url]
    except Exception:
        return []


def _row_to_properties(row):
    return {
        "crawled_url_id": int(row[0]),
        "website_id": int(row[1]),
        "url": str(row[2]),
        "content": str(row[3]),
        #"image_urls": _normalize_image_urls(row[4] if len(row) > 4 else []),
    }

def fetch_from_postgres(website_id: int | None = None):
    query = """
        SELECT crawled_url_id, website_id, url, content
        FROM scraped_content
        WHERE content IS NOT NULL
    """

    params = {}
    if website_id is not None:
        query += " AND website_id = :website_id"
        params["website_id"] = website_id

    with engine.begin() as connection:
        rows = connection.execute(text(query), params).fetchall()
        if not rows:
            return {"indexed": 0, "skipped": 0}

        collection = get_weaviate_collection()
        client = get_weaviate_client()
        indexed = 0
        skipped = 0

        with client.batch.fixed_size(batch_size=50) as batch:
            for row in rows:
                try:
                    batch.add_object(
                        collection=collection.name,
                        properties=_row_to_properties(row),
                    )
                    indexed += 1
                except Exception:
                    skipped += 1

            ids = [r[0] for r in rows]
            connection.execute(
                text("""
                    UPDATE scraped_content
                    SET indexing = TRUE
                    WHERE crawled_url_id = ANY(:ids)
                """),
                {"ids": ids},
            )

    return {"indexed": indexed, "skipped": skipped}


def index_knowledge_base(website_id: int | None = None):
    return fetch_from_postgres(website_id)
