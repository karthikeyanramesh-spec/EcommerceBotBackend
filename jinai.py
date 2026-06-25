import aiohttp
from sqlalchemy import text

async def scrape_with_jina(session, url):
    try:
        jina_url = f"https://r.jina.ai/http://{url.replace('https://', '').replace('http://', '')}"

        async with session.get(jina_url, timeout=60) as response:

            if response.status != 200:
                print(f"Failed: {url}")
                return None

            return await response.text()

    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return None
    
def get_scrap_new(db, website_id):
    query = text("""SELECT su.id, su.website_id, su.url FROM saved_urls su LEFT JOIN scraped_content sc ON sc.crawled_url_id = su.id where su.website_id = :website_id AND sc.crawled_url_id is NULL""")
    result = db.execute(query, {"website_id": website_id})
    return result.fetchall()