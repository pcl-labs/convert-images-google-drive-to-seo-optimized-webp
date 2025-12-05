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
            response = await client.get(PROXYSCRAPE_API_URL)
            if response.status_code == 200:
                proxy_list = response.text.strip().split('\n')
                for proxy in proxy_list:
                    proxy = proxy.strip()
                    if ':' in proxy and proxy:
                        # Format as http://ip:port
                        if not proxy.startswith('http'):
                            proxy = f"http://{proxy}"
                        proxies.append(proxy)
                logger.info(f"Fetched {len(proxies)} proxies from ProxyScrape")
    except Exception as e:
        logger.warning(f"Error fetching from ProxyScrape: {str(e)}")
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
    """Normalize proxy URL to http://ip:port format."""
    proxy = proxy.strip()
    if not proxy:
        return ""
    
    # Remove http:// or https:// if present
    if proxy.startswith('http://') or proxy.startswith('https://'):
        proxy = proxy.split('://', 1)[1]
    
    # Add http:// prefix
    if ':' in proxy:
        return f"http://{proxy}"
    return ""
