from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Tuple

import weaviate
from dotenv import load_dotenv
from weaviate.classes.init import AdditionalConfig, Auth, Timeout

load_dotenv()


def _get_env(name: str, fallback: str | None = None) -> str:
    value = os.getenv(name, fallback)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _candidate_collection_names() -> list[str]:
    names: list[str] = []
    for value in (
        os.getenv("WEAVIATE_COLLECTION_NAME"),
        os.getenv("COLLECTION_NAME"),
        "ScrapedContent",
        "Ecobotcollection",
    ):
        if value and value not in names:
            names.append(value)
    return names


def get_collection_name() -> str:
    return _candidate_collection_names()[0]


@lru_cache(maxsize=1)
def get_weaviate_client() -> Any:
    """Create and cache the Weaviate Cloud client once per process."""
    weaviate_url = _get_env("WEAVIATE_URL")
    weaviate_api_key = _get_env("WEAVIATE_API_KEY")

    return weaviate.connect_to_weaviate_cloud(
        cluster_url=weaviate_url,
        auth_credentials=Auth.api_key(weaviate_api_key),
        additional_config=AdditionalConfig(
            timeout=Timeout(init=10, query=90, insert=90),
        ),
        skip_init_checks=True,
    )


@lru_cache(maxsize=1)
def get_weaviate_collection() -> Any:
    """Return the cached collection handle."""
    client = get_weaviate_client()

    for collection_name in _candidate_collection_names():
        try:
            if client.collections.exists(collection_name):
                return client.collections.get(collection_name)
        except Exception:
            continue

    raise RuntimeError(
        "No matching Weaviate collection found. Tried: "
        + ", ".join(_candidate_collection_names())
    )


def get_weaviate_handles() -> Tuple[Any, Any]:
    """Return the cached Weaviate client and collection."""
    client = get_weaviate_client()
    collection = get_weaviate_collection()
    return client, collection


import os
import weaviate
from weaviate.classes.init import Auth

# Best practice: store your credentials in environment variables
weaviate_url = os.environ["WEAVIATE_URL"]
weaviate_api_key = os.environ["WEAVIATE_API_KEY"]

# Connect to Weaviate Cloud
client = weaviate.connect_to_weaviate_cloud(
    cluster_url=weaviate_url,
    auth_credentials=Auth.api_key(weaviate_api_key),
)

