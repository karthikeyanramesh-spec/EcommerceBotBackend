import json
import shutil
import subprocess

from sqlalchemy import text

from postgreconnect import engine

def _run_openbrand(url):
    if not shutil.which("node"):
        raise RuntimeError("Node.js is required to run openbrand.")

    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            'import { extractBrandAssets } from "openbrand";'
            ' const result = await extractBrandAssets(process.argv[1]);'
            ' console.log(JSON.stringify(result));',
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip() or completed.stdout.strip() or "openbrand failed"
        )

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("openbrand returned invalid JSON") from exc


def extract_branding(url):
    try:
        result = _run_openbrand(url)
        if not result.get("ok"):
            return None

        data = result.get("data") or {}
        logos = data.get("logos") or []
        colors = data.get("colors") or []

        primary_color = next(
            (
                asset["hex"]
                for asset in colors
                if str(asset.get("usage", "")).lower() == "primary" and asset.get("hex")
            ),
            next(
                (
                    asset["hex"]
                    for asset in colors
                    if asset.get("hex")
                ),
                "#8b5cf6",
            ),
        )

        return {
            "company_name": (data.get("brand_name") or "").strip(),
            "logo_url": logos[0]["url"] if logos else "",
            "favicon_url": next(
                (
                    asset["url"]
                    for asset in logos
                    if (asset.get("type") or "").lower()
                    in {"favicon", "apple-touch-icon", "icon"}
                    and asset.get("url")
                ),
                "",
            ),
            "primary_color": primary_color,
        }
    except Exception as e:
        print(e)
        return None


def save_branding(website_id, branding):
    if not branding:
        return

    with engine.begin() as conn:
        conn.execute(
            text(
                """
            DELETE FROM brand_profile
            WHERE wesite_id = :website_id
            """
            ),
            {"website_id": website_id},
        )

        conn.execute(
            text(
                """
            INSERT INTO brand_profile(
                wesite_id,
                company_name,
                logo_url,
                favicon_url,
                primary_color
            )
            VALUES(
                :website_id,
                :company_name,
                :logo_url,
                :favicon_url,
                :primary_color
            )
            """
            ),
            {
                "website_id": website_id,
                "company_name": branding["company_name"],
                "logo_url": branding["logo_url"],
                "favicon_url": branding["favicon_url"],
                "primary_color": branding["primary_color"],
            },
        )
