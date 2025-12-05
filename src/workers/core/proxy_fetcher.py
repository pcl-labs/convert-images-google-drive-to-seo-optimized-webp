"""Free proxy fetcher from public APIs."""
from __future__ import annotations

import logging
from typing import List

import httpx

logger = logging.getLogger(__name__)

PROXYSCRAPE_API_URL = "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"


async def fetch_proxyscrape_proxies(timeout: float = 10.0) -> List[str]:
    """Fetch free proxies from ProxyScrape API."""
    proxies: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Disable automatic decompression to handle raw response
            response = await client.get(
                PROXYSCRAPE_API_URL,
                headers={"Accept-Encoding": "identity"}  # Request no compression
            )
            if response.status_code == 200:
                # Try to decode as text, handling both compressed and uncompressed
                try:
                    text = response.text
                except Exception:
                    # If text decoding fails, try to decompress manually
                    import gzip
                    text = gzip.decompress(response.content).decode('utf-8')
                
                proxy_list = text.strip().split('\n')
                for proxy in proxy_list:
                    proxy = normalize_proxy_url(proxy)
                    if proxy:
                        proxies.append(proxy)
                logger.info(f"Fetched {len(proxies)} proxies from ProxyScrape")
            else:
                logger.warning(f"ProxyScrape API returned status {response.status_code}")
    except httpx.TimeoutException as e:
        logger.warning(f"Timeout fetching from ProxyScrape: {str(e)}")
    except httpx.HTTPError as e:
        logger.warning(f"HTTP error fetching from ProxyScrape: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error fetching from ProxyScrape: {str(e)}", exc_info=True)
    return proxies


async def fetch_all_free_proxies(timeout: float = 10.0) -> List[str]:
    """Fetch proxies from all available free sources."""
    all_proxies: List[str] = []
    
    # Fetch from ProxyScrape
    proxyscrape_proxies = await fetch_proxyscrape_proxies(timeout)
    all_proxies.extend(proxyscrape_proxies)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_proxies = []
    for proxy in all_proxies:
        if proxy not in seen:
            seen.add(proxy)
            unique_proxies.append(proxy)
    
    logger.info(f"Total unique proxies fetched: {len(unique_proxies)}")
    return unique_proxies


def normalize_proxy_url(proxy: str) -> str:
    """Normalize proxy URL while preserving scheme if provided."""
    proxy = proxy.strip()
    if not proxy:
        return ""

    scheme = "http"
    if proxy.startswith("https://"):
        scheme = "https"
    if proxy.startswith("http://") or proxy.startswith("https://"):
        proxy = proxy.split("://", 1)[1]

    if ':' in proxy:
        return f"{scheme}://{proxy}"
    return ""
