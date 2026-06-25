from __future__ import annotations

from typing import Any, Iterable, Optional

from dotenv import load_dotenv
from sqlalchemy import text

from weaviate.collections.classes.filters import Filter

try:
    from weaviateconnect import get_weaviate_client, get_weaviate_collection
except ImportError:  # pragma: no cover - fallback for package-style imports
    from Backend.weaviateconnect import (  # type: ignore
        get_weaviate_client,
        get_weaviate_collection,
    )
import re

load_dotenv()

def _get_property(properties: Any, name: str, default: Any = None) -> Any:
    if properties is None:
        return default
    if isinstance(properties, dict):
        return properties.get(name, default)
    if hasattr(properties, name):
        return getattr(properties, name)
    try:
        return properties[name]
    except Exception:
        return default


def _row_to_document(row: Any) -> dict[str, Any]:
    return {
        "crawled_url_id": int(row[0]),
        "website_id": int(row[1]),
        "url": str(row[2]),
        "content": str(row[3]),
        #"image_urls": [str(url) for url in image_urls if url],
    }


def _existing_document_keys(collection: Any) -> set[tuple[int, str]]:
    """Load existing keys once so indexing can batch efficiently."""
    existing: set[tuple[int, str]] = set()
    offset = 0
    page_size = 200

    while True:
        result = collection.query.fetch_objects(
            limit=page_size,
            offset=offset,
            return_properties=True,
        )
        if not result.objects:
            break

        for obj in result.objects:
            properties = obj.properties
            website_id = _get_property(properties, "website_id")
            url = _get_property(properties, "url")
            if website_id is None or not url:
                continue
            existing.add((int(website_id), str(url)))

        if len(result.objects) < page_size:
            break
        offset += page_size

    return existing


def index_knowledge_base(website_id: Optional[int] = None) -> dict[str, int]:
    from postgreconnect import engine

    query = """
        SELECT
            crawled_url_id,
            website_id,
            url,
            content
        FROM scraped_content
        WHERE content IS NOT NULL
    """
    params: dict[str, Any] = {}
    if website_id is not None:
        query += " AND website_id = :website_id"
        params["website_id"] = website_id

    with engine.connect() as connection:
        rows = connection.execute(text(query), params).fetchall()

    if not rows:
        return {"indexed": 0, "skipped": 0}

    collection = get_weaviate_collection()
    client = get_weaviate_client()
    existing_keys = _existing_document_keys(collection)

    indexed = 0
    skipped = 0

    with client.batch.fixed_size(batch_size=50, concurrent_requests=2) as batch:
        for row in rows:
            document = _row_to_document(row)
            key = (document["website_id"], document["url"])

            if key in existing_keys:
                skipped += 1
                continue

            try:
                batch.add_object(
                    collection=collection.name,
                    properties=document,
                )
                indexed += 1
                existing_keys.add(key)
            except Exception as exc:
                skipped += 1
                print(
                    f"[TRAIN] Skipping document website_id={document['website_id']} "
                    f"url={document['url']}: {exc}"
                )

    if website_id is not None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE saved_urls
                    SET trained = TRUE
                    WHERE website_id = :website_id
                    """
                ),
                {"website_id": website_id},
            )

    return {"indexed": indexed, "skipped": skipped}


def retrieve_context(
    question: str,
    website_id: int | None = None,
    limit: int = 5,
) -> list[dict[str, str]]:
    try:
        collection = get_weaviate_collection()

        filters = None
        if website_id is not None:
            filters = Filter.by_property("website_id").equal(website_id)

        result = collection.query.near_text(
            query=question,
            limit=limit,
            filters=filters,
            return_metadata=["distance"],
        )
    except Exception as exc:
        print(f"[RAG] Retrieval failed: {exc}")
        return []

    documents: list[dict[str, str]] = []
    for obj in result.objects:
        properties = obj.properties
        url = _get_property(properties, "url", "")
        content = _get_property(properties, "content", "")
        if not content:
            continue
        documents.append(
            {
                "url": str(url),
                "content": str(content),}
        )

    return documents

def build_context(question: str, website_id: int | None = None, limit: int =5,) -> str:
    documents = retrieve_context(question, website_id = website_id, limit=limit,)
    if not documents:
        return "No relevant information found."
    blocks = []
    for document in documents:
        blocks.append(
             f"URL:\n{document['url']}\n\n"
            f"CONTENT:\n{document['content']}\n\n")
    return "\n".join(blocks)



def extract_images_from_context(text):
    pattern = r'https?://[^\s\)\]]+\.(?:jpg|jpeg|png|webp)(?:\?[^\s\)\]]*)?'
    return list(set(re.findall(pattern, text)))
def build_rag_prompt (question: str, website_id: int | None = None, limit: int=5) -> str:

    documents = retrieve_context(
    question,
    website_id=website_id,
    limit=limit
)

    context = build_context(
        question,
        website_id,
        limit
    )

    images = []
    seen = set()

    for doc in documents:
        urls = extract_images_from_context(doc["content"])

        for url in urls:
            if url in seen:
                continue

            seen.add(url)
            images.append(url)

            if len(images) >= 4:
                break

        if len(images) >= 4:
            break
    images = [
    url for url in images
    if not any(
        word in url.lower()
        for word in [
            "logo",
            "badge",
            "icon",
            "payment",
            "shipping",
            "verified",
            "checkmark",
            "arrow",
            "powered_by",
            "upi",
            "phonepe",
            "gpay",
            "cart"
        ]
    )
]
    prompt = f"""
Answer the user's question using ONLY the information provided in CONTEXT.

STRICT RULES:

- CONTEXT is the only source of truth.
- Never use outside knowledge.
- Never invent products.
- Never invent brands.
- Never invent prices.
- Never invent specifications.
- Never invent availability.
- Never invent policies.
- Never invent categories.
- Use only information explicitly present in CONTEXT.

If the answer cannot be found in CONTEXT, respond exactly:

"I could not find this information in my knowledge base."

If the question is about a product detected from the camera:

1. Identify the detected product from the user's question.
2. Search ONLY within CONTEXT for products that are most similar to the detected product.
3. Rank products by semantic similarity (same category, type, features, use case, or attributes mentioned in CONTEXT).
4. Return ONLY the TOP 5 most similar products found in CONTEXT.
5. Do not return more than 5 products.
6. Do not include products that are not present in CONTEXT.
7. Do not invent similarities or product details.
8. If fewer than 5 similar products exist, return only the available matches.

If no similar products exist in CONTEXT, respond exactly:

"We currently do not have a similar product available in our catalog."

CONTEXT:
{context}

QUESTION:
{question}
"""
    print(limit,"LIMIT")
    return prompt, images
