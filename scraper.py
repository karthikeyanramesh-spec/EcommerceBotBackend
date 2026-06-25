import asyncio
import json
import aiohttp
import re
from urllib.parse import urljoin
from sqlalchemy import text
from jinai import get_scrap_new, scrape_with_jina
from postgreconnect import engine
from branding import save_branding, extract_branding

async def scrape_content(db, website_id):
    urls = get_scrap_new(db, website_id)
    if not urls:
        return {
            "website_id": website_id,
            "scraped": 0,
            "message": "No new URLs found"
        }

    async with aiohttp.ClientSession() as session:
        tasks = [scrape_with_jina(session, row.url) for row in urls]
        contents = await asyncio.gather(*tasks)

    inserted = 0

    for row, content in zip(urls, contents):
        if not content:
            continue

        #image_urls = extract_image_urls(content, row.url)
        db.execute(text(
            """
            INSERT INTO scraped_content(
                crawled_url_id,
                website_id,
                url,
                content
            )
            VALUES(
                :crawled_url_id,
                :website_id,
                :url,
                :content
            )
            """
        ), {
            "crawled_url_id": row.id,
            "website_id": row.website_id,
            "url": row.url,
            "content": content,
            #"image_urls": json.dumps(image_urls),
        })
        db.execute(
            text("""UPDATE saved_urls SET trained = TRUE WHERE id=:id"""),
            {"id": row.id}
        )
        inserted +=1

    return{
       "website_id": website_id,
        "new_urls": len(urls),
        "scraped": inserted 
    }


