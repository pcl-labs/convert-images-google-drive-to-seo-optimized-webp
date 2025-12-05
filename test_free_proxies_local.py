#!/usr/bin/env python3
"""
Local test script for free proxy functionality.

Tests the proxy fetcher and proxy pool manager locally.

Usage:
    python test_free_proxies_local.py
"""

import asyncio
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add src/workers to path so we can import
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root / "src" / "workers"))

# Mock settings before importing proxy_pool
class MockSettings:
    youtube_scraper_proxy_pool = []
    youtube_scraper_enable_free_proxies = False
    youtube_scraper_proxy_fetch_interval_minutes = 60
    youtube_scraper_proxy_health_check_interval_minutes = 30
    youtube_scraper_max_free_proxies = 50
    youtube_scraper_proxy_health_check_timeout = 5.0
    youtube_scraper_proxy_min_success_rate = 0.3
    youtube_scraper_proxy_rotation_strategy = "random"

# Mock the api.config.settings import
with patch.dict('sys.modules', {'api.config': MagicMock(settings=MockSettings())}):
    from core.proxy_fetcher import fetch_all_free_proxies, normalize_proxy_url
    from core.proxy_pool import ProxyPoolManager, ProxyEntry


async def test_proxy_fetcher():
    """Test fetching proxies from ProxyScrape."""
    print("=" * 80)
    print("Testing Proxy Fetcher")
    print("=" * 80)
    
    try:
        proxies = await fetch_all_free_proxies(timeout=15.0)
        print(f"\n✅ Successfully fetched {len(proxies)} proxies")
        
        if proxies:
            print(f"\nFirst 5 proxies:")
            for i, proxy in enumerate(proxies[:5], 1):
                print(f"  {i}. {proxy}")
        else:
            print("\n⚠️  No proxies fetched (this might be normal if ProxyScrape is down)")
        
        return proxies
    except Exception as e:
        print(f"\n❌ Error fetching proxies: {str(e)}")
        import traceback
        traceback.print_exc()
        return []


async def test_proxy_normalization():
    """Test proxy URL normalization."""
    print("\n" + "=" * 80)
    print("Testing Proxy URL Normalization")
    print("=" * 80)
    
    test_cases = [
        ("192.168.1.1:8080", "http://192.168.1.1:8080"),
        ("http://192.168.1.1:8080", "http://192.168.1.1:8080"),
        ("https://192.168.1.1:8080", "http://192.168.1.1:8080"),
        ("", ""),
    ]
    
    all_passed = True
    for input_proxy, expected in test_cases:
        result = normalize_proxy_url(input_proxy)
        status = "✅" if result == expected else "❌"
        print(f"{status} '{input_proxy}' -> '{result}' (expected: '{expected}')")
        if result != expected:
            all_passed = False
    
    return all_passed


async def test_proxy_pool_manager():
    """Test the proxy pool manager."""
    print("\n" + "=" * 80)
    print("Testing Proxy Pool Manager")
    print("=" * 80)
    
    try:
        # Create a manager with test settings
        manager = ProxyPoolManager()
        
        # Override settings for testing
        manager.max_proxies = 10
        manager.health_check_timeout = 3.0
        manager.fetch_interval = 0  # Allow immediate refresh
        
        print(f"\nInitial pool size: {len(manager.proxies)}")
        
        # Test fetching new proxies
        print("\nFetching new proxies...")
        await manager.refresh_pool()
        print(f"Pool size after fetch: {len(manager.proxies)}")
        
        # Test getting a proxy
        proxy = manager.get_next_proxy()
        if proxy:
            print(f"\n✅ Got proxy: {proxy}")
        else:
            print("\n⚠️  No proxy available (pool might be empty)")
        
        # Test pool stats
        stats = manager.get_pool_stats()
        print(f"\nPool Stats:")
        print(f"  Total proxies: {stats['total_proxies']}")
        print(f"  Active proxies: {stats['active_proxies']}")
        print(f"  Inactive proxies: {stats['inactive_proxies']}")
        
        # Test health check (optional - might take a while)
        print("\n⚠️  Skipping health check (takes time, enable manually if needed)")
        # Uncomment to test health checking:
        # print("\nPerforming health check...")
        # await manager.health_check_all()
        # stats = manager.get_pool_stats()
        # print(f"Active proxies after health check: {stats['active_proxies']}")
        
        return True
    except Exception as e:
        print(f"\n❌ Error testing proxy pool manager: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


async def test_proxy_with_youtube():
    """Test using a proxy to access YouTube (optional, slow)."""
    print("\n" + "=" * 80)
    print("Testing Proxy with YouTube (Optional - Slow)")
    print("=" * 80)
    
    try:
        import httpx
        
        # Fetch a few proxies
        proxies = await fetch_all_free_proxies(timeout=10.0)
        if not proxies:
            print("⚠️  No proxies available to test")
            return False
        
        # Test first proxy
        test_proxy = proxies[0]
        print(f"\nTesting proxy: {test_proxy}")
        
        try:
            async with httpx.AsyncClient(
                proxies={"http://": test_proxy, "https://": test_proxy},
                timeout=10.0,
                verify=False,
            ) as client:
                response = await client.get("https://www.youtube.com", follow_redirects=True)
                if response.status_code == 200:
                    print(f"✅ Proxy works! Status: {response.status_code}")
                    return True
                else:
                    print(f"⚠️  Proxy returned status: {response.status_code}")
                    return False
        except Exception as e:
            print(f"❌ Proxy test failed: {str(e)}")
            return False
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print("Free Proxy Functionality - Local Test")
    print("=" * 80)
    print("\nThis script tests the free proxy fetcher and pool manager locally.")
    print("Note: Some tests require internet connection and may take time.\n")
    
    results = {}
    
    # Test 1: Proxy normalization (fast)
    results["normalization"] = await test_proxy_normalization()
    
    # Test 2: Proxy fetcher (requires internet)
    results["fetcher"] = await test_proxy_fetcher()
    
    # Test 3: Proxy pool manager
    results["pool_manager"] = await test_proxy_pool_manager()
    
    # Test 4: Test with YouTube (optional, slow)
    print("\n" + "=" * 80)
    try:
        response = input("Test proxy with YouTube? (slow, y/N): ").strip().lower()
        if response == 'y':
            results["youtube_test"] = await test_proxy_with_youtube()
    except (EOFError, KeyboardInterrupt):
        print("\nSkipping YouTube test (non-interactive mode)")
    
    # Summary
    print("\n" + "=" * 80)
    print("Test Summary")
    print("=" * 80)
    for test_name, result in results.items():
        if isinstance(result, bool):
            status = "✅ PASS" if result else "❌ FAIL"
        elif isinstance(result, list):
            status = f"✅ PASS ({len(result)} proxies)"
        else:
            status = f"✅ PASS" if result else "❌ FAIL"
        print(f"{test_name:20} {status}")
    
    print("\n" + "=" * 80)
    print("Done!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
