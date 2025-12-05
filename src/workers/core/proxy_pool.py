"""Proxy pool manager with health checking and rotation."""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from api.config import settings
from .proxy_fetcher import fetch_all_free_proxies, normalize_proxy_url

logger = logging.getLogger(__name__)


@dataclass
class ProxyEntry:
    """Represents a single proxy with statistics."""
    url: str
    success_count: int = 0
    failure_count: int = 0
    last_used: Optional[datetime] = None
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    is_active: bool = True
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0
    
    @property
    def total_attempts(self) -> int:
        """Total number of attempts."""
        return self.success_count + self.failure_count


class ProxyPoolManager:
    """Manages a pool of free proxies with health checking."""
    
    def __init__(self) -> None:
        self.proxies: Dict[str, ProxyEntry] = {}
        self.last_fetch: Optional[datetime] = None
        self.last_health_check: Optional[datetime] = None
        self.rotation_index: int = 0
        
        # Configuration from settings
        self.fetch_interval = getattr(settings, 'youtube_scraper_proxy_fetch_interval_minutes', 60) * 60
        self.health_check_interval = getattr(settings, 'youtube_scraper_proxy_health_check_interval_minutes', 30) * 60
        self.max_proxies = getattr(settings, 'youtube_scraper_max_free_proxies', 50)
        self.health_check_timeout = getattr(settings, 'youtube_scraper_proxy_health_check_timeout', 5.0)
        self.min_success_rate = getattr(settings, 'youtube_scraper_proxy_min_success_rate', 0.3)
        self.rotation_strategy = getattr(settings, 'youtube_scraper_proxy_rotation_strategy', 'random')
        
        # Load manual proxies from settings
        manual_proxies = getattr(settings, 'youtube_scraper_proxy_pool', []) or []
        for proxy_url in manual_proxies:
            normalized = normalize_proxy_url(proxy_url)
            if normalized:
                self.proxies[normalized] = ProxyEntry(url=normalized)
    
    async def _check_proxy_health(self, proxy_url: str) -> bool:
        """Check if a proxy is working by testing it against YouTube."""
        try:
            async with httpx.AsyncClient(
                proxies={"http://": proxy_url, "https://": proxy_url},
                timeout=self.health_check_timeout,
                verify=False,  # Free proxies often have SSL issues
            ) as client:
                # Test with a simple YouTube request
                response = await client.get(
                    "https://www.youtube.com",
                    follow_redirects=True,
                )
                return response.status_code == 200
        except Exception as e:
            logger.debug(f"Proxy health check failed for {proxy_url}: {str(e)}")
            return False
    
    async def refresh_pool(self) -> None:
        """Fetch new proxies and add them to the pool."""
        current_time = datetime.now(timezone.utc)
        
        # Check if we need to refresh
        if self.last_fetch:
            elapsed = (current_time - self.last_fetch).total_seconds()
            if elapsed < self.fetch_interval:
                return
        
        logger.info("Refreshing proxy pool...")
        try:
            new_proxies = await fetch_all_free_proxies(timeout=10.0)
            added_count = 0
            
            for proxy_url in new_proxies:
                normalized = normalize_proxy_url(proxy_url)
                if not normalized:
                    continue
                
                # Don't add if we already have it
                if normalized in self.proxies:
                    continue
                
                # Don't exceed max proxies
                if len(self.proxies) >= self.max_proxies:
                    break
                
                # Add proxy (will be validated on first use)
                self.proxies[normalized] = ProxyEntry(url=normalized)
                added_count += 1
            
            self.last_fetch = current_time
            logger.info(f"Added {added_count} new proxies to pool (total: {len(self.proxies)})")
        except Exception as e:
            logger.error(f"Error refreshing proxy pool: {str(e)}")
    
    async def validate_proxy(self, proxy_url: str) -> bool:
        """Validate a single proxy."""
        return await self._check_proxy_health(proxy_url)
    
    async def health_check_all(self) -> None:
        """Perform health check on all proxies."""
        current_time = datetime.now(timezone.utc)
        
        # Check if we need to health check
        if self.last_health_check:
            elapsed = (current_time - self.last_health_check).total_seconds()
            if elapsed < self.health_check_interval:
                return
        
        logger.info(f"Performing health check on {len(self.proxies)} proxies...")
        working_count = 0
        
        # Check proxies concurrently (limit to 10 at a time)
        proxy_list = list(self.proxies.values())
        for i in range(0, len(proxy_list), 10):
            batch = proxy_list[i:i + 10]
            tasks = [self._check_proxy_health(proxy.url) for proxy in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for proxy, is_working in zip(batch, results):
                if isinstance(is_working, Exception) or not is_working:
                    proxy.is_active = False
                    proxy.last_failure = current_time
                    proxy.failure_count += 1
                else:
                    proxy.is_active = True
                    proxy.last_success = current_time
                    proxy.success_count += 1
                    working_count += 1
        
        # Remove proxies with low success rate
        to_remove = []
        for url, proxy in self.proxies.items():
            if proxy.total_attempts >= 5 and proxy.success_rate < self.min_success_rate:
                to_remove.append(url)
        
        for url in to_remove:
            del self.proxies[url]
            logger.debug(f"Removed proxy with low success rate: {url}")
        
        self.last_health_check = current_time
        logger.info(f"Health check complete: {working_count}/{len(self.proxies)} proxies working")
    
    def get_next_proxy(self) -> Optional[str]:
        """Get the next proxy to use based on rotation strategy."""
            # Filter to active proxies only
            active_proxies = [
                proxy for proxy in self.proxies.values()
                if proxy.is_active
            ]
            
            if not active_proxies:
                # Try inactive proxies if no active ones
                active_proxies = list(self.proxies.values())
            
            if not active_proxies:
                return None
            
            # Select based on rotation strategy
            if self.rotation_strategy == "random":
                proxy = random.choice(active_proxies)
            elif self.rotation_strategy == "round_robin":
                proxy = active_proxies[self.rotation_index % len(active_proxies)]
                self.rotation_index += 1
            elif self.rotation_strategy == "best":
                # Sort by success rate, then by total attempts
                proxy = max(
                    active_proxies,
                    key=lambda p: (p.success_rate, p.total_attempts)
                )
            else:  # lru or default
                # Least recently used
                proxy = min(
                    active_proxies,
                    key=lambda p: p.last_used or datetime.min.replace(tzinfo=timezone.utc)
                )
            
            proxy.last_used = datetime.now(timezone.utc)
            return proxy.url
    
    def mark_proxy_success(self, proxy_url: str) -> None:
        """Mark a proxy as successful."""
        if proxy_url in self.proxies:
            proxy = self.proxies[proxy_url]
            proxy.success_count += 1
            proxy.last_success = datetime.now(timezone.utc)
            proxy.is_active = True
    
    def mark_proxy_failure(self, proxy_url: str) -> None:
        """Mark a proxy as failed."""
        if proxy_url in self.proxies:
            proxy = self.proxies[proxy_url]
            proxy.failure_count += 1
            proxy.last_failure = datetime.now(timezone.utc)
            # Don't immediately mark as inactive, let health check decide
    
    def get_pool_stats(self) -> Dict[str, any]:
        """Get statistics about the proxy pool."""
        active = sum(1 for p in self.proxies.values() if p.is_active)
        total = len(self.proxies)
        avg_success_rate = (
            sum(p.success_rate for p in self.proxies.values()) / total
            if total > 0 else 0.0
        )
        return {
            "total_proxies": total,
            "active_proxies": active,
            "inactive_proxies": total - active,
            "average_success_rate": avg_success_rate,
            "last_fetch": self.last_fetch.isoformat() if self.last_fetch else None,
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
        }


# Global proxy pool manager instance
_proxy_pool_manager: Optional[ProxyPoolManager] = None


def get_proxy_pool_manager() -> ProxyPoolManager:
    """Get or create the global proxy pool manager."""
    global _proxy_pool_manager
    if _proxy_pool_manager is None:
        _proxy_pool_manager = ProxyPoolManager()
    return _proxy_pool_manager
