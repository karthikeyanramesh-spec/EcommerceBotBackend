from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy import text
from branding import save_branding, extract_branding
from scraper import scrape_content
from urllib.parse import urlparse, urljoin
import asyncio
from postgreconnect import engine
from crawling import UltraCrawler
from posttoweavi import index_knowledge_base
import uvicorn
from gemini import gemini_session_handler
from fastapi import WebSocket
class CrawlRequest(BaseModel):
    url: str
    max_urls: Optional[int] = 100000

class SaveUrlsRequest(BaseModel):
    source_url: str
    urls: List[str]

class ScrapeRequest(BaseModel):
    website_id: int


class TrainRequest(BaseModel):
    website_id: Optional[int] = None
class MicStateRequest(BaseModel):
    action: str
    website_id: Optional[int] = None

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def normalize_url(start_url: str) -> str:
    start_url = start_url.strip()
    if not start_url:
        raise ValueError("URL is required")

    if not start_url.startswith(("http://", "https://")):
        start_url = "https://" + start_url

    return start_url


def crawl_site(start_url: str, max_urls: int = 100000) -> List[str]:
    start_url = normalize_url(start_url)
    crawler = UltraCrawler(start_url, max_urls=max_urls)
    crawler.start()
    return sorted(crawler.visited)


def save_urls_to_db(source_url, urls):

    source_url = normalize_url(source_url)

    with engine.begin() as conn:

        existing_website = conn.execute(
            text("""
                SELECT id
                FROM website
                WHERE base_url = :base_url
                LIMIT 1
            """),
            {"base_url": source_url}
        ).scalar_one_or_none()

        website_id = existing_website

        if website_id is None:
            website_result = conn.execute(
                text("""
                    INSERT INTO website(
                        base_url,
                        created_at
                    )
                    VALUES(
                        :base_url,
                        NOW()
                    )
                    RETURNING id
                """),
                {"base_url": source_url}
            )

            website_id = website_result.scalar_one()

        if urls:
            conn.execute(
                text("""
                    INSERT INTO saved_urls(
                        website_id,
                        url,
                        selected_for_training,
                        trained,
                        created_at
                    )
                    VALUES(
                        :website_id,
                        :url,
                        FALSE,
                        FALSE,
                        NOW()
                    )
                """),
                [
                    {
                        "website_id": website_id,
                        "url": url
                    }
                    for url in urls
                ]
            )

    return website_id, len(urls)


async def train_website_knowledge_base(website_id: int) -> dict[str, int]:
    website: dict | None = None

    with engine.begin() as db:
        website = db.execute(
            text("""
            SELECT base_url
            FROM website
            WHERE id = :website_id
            """),
            {"website_id": website_id},
        ).mappings().first()

        if not website:
            raise HTTPException(status_code=404, detail="Data source not found")

        scrape_result = await scrape_content(db, website_id)

    train_stats = await asyncio.to_thread(
        index_knowledge_base,
        website_id,
    )

    if website:
        try:
            branding = extract_branding(website["base_url"])
            if branding:
                save_branding(website_id, branding)
        except Exception as exc:
            print(f"Branding update failed: {exc}")

    return {
        "website_id": website_id,
        "scraped": scrape_result["scraped"],
        "new_urls": scrape_result["new_urls"],
        "indexed": train_stats["indexed"],
        "skipped": train_stats["skipped"],
    }


@app.get("/data-sources")
async def list_data_sources():
    query = text(
        """
        WITH source_ids AS (
            SELECT id AS website_id
            FROM website
            UNION
            SELECT DISTINCT website_id
            FROM saved_urls
            UNION
            SELECT DISTINCT website_id
            FROM scraped_content
        )
        SELECT
            s.website_id,
            COALESCE(
                w.base_url,
                (
                    SELECT sc.url
                    FROM scraped_content sc
                    WHERE sc.website_id = s.website_id
                    ORDER BY sc.id DESC
                    LIMIT 1
                ),
                (
                    SELECT su.url
                    FROM saved_urls su
                    WHERE su.website_id = s.website_id
                    ORDER BY su.id DESC
                    LIMIT 1
                )
            ) AS base_url,
            COALESCE(
                bp.company_name,
                w.base_url,
                (
                    SELECT sc.url
                    FROM scraped_content sc
                    WHERE sc.website_id = s.website_id
                    ORDER BY sc.id DESC
                    LIMIT 1
                ),
                (
                    SELECT su.url
                    FROM saved_urls su
                    WHERE su.website_id = s.website_id
                    ORDER BY su.id DESC
                    LIMIT 1
                )
            ) AS company_name,
            bp.logo_url,
            bp.favicon_url,
            bp.primary_color,
            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM saved_urls su
                    WHERE su.website_id = s.website_id AND su.trained = TRUE
                ) THEN 'Trained'
                WHEN EXISTS (
                    SELECT 1
                    FROM scraped_content sc
                    WHERE sc.website_id = s.website_id
                ) THEN 'Trained'
                WHEN EXISTS (
                    SELECT 1
                    FROM saved_urls su
                    WHERE su.website_id = s.website_id
                ) THEN 'Saved'
                ELSE 'New'
            END AS status
        FROM source_ids s
        LEFT JOIN website w
            ON w.id = s.website_id
        LEFT JOIN brand_profile bp
            ON bp.wesite_id = s.website_id
        ORDER BY
            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM saved_urls su
                    WHERE su.website_id = s.website_id AND su.trained = TRUE
                ) THEN 0
                WHEN EXISTS (
                    SELECT 1
                    FROM scraped_content sc
                    WHERE sc.website_id = s.website_id
                ) THEN 0
                WHEN EXISTS (
                    SELECT 1
                    FROM saved_urls su
                    WHERE su.website_id = s.website_id
                ) THEN 1
                ELSE 2
            END,
            s.website_id DESC
        """
    )

    with engine.begin() as conn:
        sources = conn.execute(query).mappings().all()

    return {"sources": [dict(source) for source in sources]}


@app.post("/crawl")
async def api_crawl(request: CrawlRequest):
    try:
        urls = await asyncio.to_thread(
            crawl_site,
            request.url,
            request.max_urls or 100000,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Crawl failed: {exc}")

    return {"urls": urls, "count": len(urls)}


@app.post("/save_urls")
async def api_save_urls(request: SaveUrlsRequest):
    try:
        website_id, inserted = await asyncio.to_thread(
            save_urls_to_db,
            request.source_url,
            request.urls,
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Save failed: {exc}")

    return {
        "website_id": website_id,
        "inserted": inserted,
        "total": len(request.urls)
    }


@app.post("/api/train")
async def api_train(request: TrainRequest | None = None):
    try:
        website_id = request.website_id if request else None
        if website_id is None:
            raise HTTPException(
                status_code=400,
                detail="website_id is required to train a data source",
            )

        stats = await train_website_knowledge_base(website_id)
        return {
            "status": "success",
            **stats,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Training failed: {exc}")

@app.post("/voice/mic")
async def voice_mic_state(request: MicStateRequest):
    action = (request.action or "").strip().lower()
    if action not in {"start", "stop"}:
        raise HTTPException(status_code=400, detail="action must be start or stop")

    return {
        "status": "ok",
        "action": action,
        "website_id": request.website_id,
    }

@app.post("/scrape-content")
async def scrape_website_content(request: ScrapeRequest):
    stats = await train_website_knowledge_base(request.website_id)
    return stats


@app.get("/branding/{website_id}")
async def get_branding(website_id: int):
    with engine.begin() as db:
        branding = db.execute(
            text("""
            SELECT
                wesite_id AS website_id,
                company_name,
                logo_url,
                favicon_url,
                primary_color
            FROM brand_profile
            WHERE wesite_id = :website_id
            ORDER BY wesite_id DESC
            LIMIT 1
            """),
            {"website_id": website_id}
        ).mappings().first()

    if branding and branding.get("primary_color"):
        return dict(branding)

    with engine.begin() as db:
        website = db.execute(
            text("""
            SELECT base_url
            FROM website
            WHERE id = :website_id
            """),
            {"website_id": website_id}
        ).mappings().first()

    if website:
        try:
            refreshed_branding = extract_branding(website["base_url"])
            if refreshed_branding:
                save_branding(website_id, refreshed_branding)

            with engine.begin() as db:
                branding = db.execute(
                    text("""
                    SELECT
                        wesite_id AS website_id,
                        company_name,
                        logo_url,
                        favicon_url,
                        primary_color
                    FROM brand_profile
                    WHERE wesite_id = :website_id
                    ORDER BY wesite_id DESC
                    LIMIT 1
                    """),
                    {"website_id": website_id}
                ).mappings().first()
        except Exception as exc:
            print(f"Branding refresh failed: {exc}")

    if not branding:
        raise HTTPException(status_code=404, detail="Branding not found")

    return dict(branding)

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket
):
    await websocket.accept()

    try:
        await gemini_session_handler(
            websocket
        )
    except Exception as e:
        print(e)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)

