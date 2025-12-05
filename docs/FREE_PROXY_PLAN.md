# Free Proxy List Integration Plan

## Overview
Implement automatic fetching, validation, and rotation of free proxy lists for YouTube scraping. This will make the proxy system more resilient and self-maintaining.

## Architecture

### Components

1. **Proxy List Fetcher** (`proxy_fetcher.py`)
   - Fetches proxies from multiple free proxy list APIs
   - Sources:
     - ProxyScrape API: `https://api.proxyscrape.com/v2/?request=get&protocol=http`
     - FreeProxyList: `https://www.proxy-list.download/api/v1/get?type=http`
     - Geonode: `https://proxylist.geonode.com/api/proxy-list?protocols=http&limit=500`
   - Parses and normalizes proxy formats
   - Returns list of `ip:port` or `http://ip:port` format

2. **Proxy Health Checker** (`proxy_health.py`)
   - Validates proxies by making test requests
   - Checks:
     - Connectivity (can connect to proxy)
     - Speed (response time < threshold)
     - Anonymity (proxy actually works)
     - YouTube compatibility (can reach youtube.com)
   - Tracks success/failure rates per proxy
   - Removes dead proxies automatically

3. **Proxy Pool Manager** (`proxy_pool.py`)
   - Maintains active pool of validated proxies
   - Implements rotation strategies:
     - Round-robin
     - Random
     - Least-recently-used
     - Best-performing (by success rate)
   - Tracks proxy statistics:
     - Success count
     - Failure count
     - Last used timestamp
     - Average response time
   - Auto-refreshes pool when too many proxies fail
   - Handles proxy failures gracefully

4. **Integration with Existing Code**
   - Update `youtube_proxy.py` to use proxy pool manager
   - Maintain backward compatibility with manual proxy lists
   - Add configuration options

## Configuration Settings

Add to `config.py`:

```python
# Free proxy list configuration
youtube_scraper_enable_free_proxies: bool = False
youtube_scraper_proxy_fetch_interval_minutes: int = 60
youtube_scraper_proxy_health_check_interval_minutes: int = 30
youtube_scraper_max_free_proxies: int = 50
youtube_scraper_proxy_health_check_timeout: float = 5.0
youtube_scraper_proxy_min_success_rate: float = 0.3  # 30% success rate minimum
youtube_scraper_proxy_rotation_strategy: str = "random"  # random, round_robin, lru, best
```

## Implementation Steps

### Step 1: Create Proxy Fetcher Module
- File: `src/workers/core/proxy_fetcher.py`
- Functions:
  - `fetch_proxyscrape_proxies() -> List[str]`
  - `fetch_geonode_proxies() -> List[str]`
  - `fetch_all_free_proxies() -> List[str]`
  - `normalize_proxy_url(proxy: str) -> str`

### Step 2: Create Proxy Health Checker
- File: `src/workers/core/proxy_health.py`
- Functions:
  - `check_proxy_health(proxy_url: str, timeout: float) -> bool`
  - `check_proxy_speed(proxy_url: str) -> Optional[float]`
  - `check_youtube_compatibility(proxy_url: str) -> bool`
  - `validate_proxy_list(proxies: List[str], max_workers: int = 10) -> List[str]`

### Step 3: Create Proxy Pool Manager
- File: `src/workers/core/proxy_pool.py`
- Class: `ProxyPoolManager`
  - Methods:
    - `__init__(config)`
    - `get_next_proxy() -> Optional[str]`
    - `mark_proxy_failed(proxy_url: str)`
    - `mark_proxy_success(proxy_url: str)`
    - `refresh_pool() -> None`
    - `get_pool_stats() -> Dict`

### Step 4: Update Configuration
- Add new settings to `config.py`
- Update `__post_init__` to validate settings
- Add environment variable parsing

### Step 5: Integrate with YouTube Proxy
- Update `_pick_proxy()` to use `ProxyPoolManager`
- Maintain fallback to manual proxy list
- Add logging for proxy selection

### Step 6: Add Background Tasks (Optional)
- Periodic proxy list refresh
- Periodic health checks
- Automatic pool maintenance

## Data Structures

### Proxy Entry
```python
@dataclass
class ProxyEntry:
    url: str
    success_count: int = 0
    failure_count: int = 0
    last_used: Optional[datetime] = None
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    avg_response_time: float = 0.0
    is_active: bool = True
    
    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0
```

### Proxy Pool State
```python
@dataclass
class ProxyPoolState:
    proxies: List[ProxyEntry]
    last_fetch: Optional[datetime] = None
    last_health_check: Optional[datetime] = None
    rotation_index: int = 0
```

## Error Handling

- Network errors during fetch: Log and continue with existing pool
- Health check failures: Mark proxy as failed, don't remove immediately
- All proxies fail: Fall back to no-proxy mode, log warning
- Rate limiting: Implement exponential backoff for proxy fetches

## Performance Considerations

- Use async/await for concurrent health checks
- Limit concurrent health checks (max 10-20 at once)
- Cache validated proxies for reasonable time period
- Don't block requests while refreshing pool

## Security Considerations

- Validate proxy URLs to prevent SSRF
- Don't trust free proxies with sensitive data
- Implement timeout limits
- Rate limit proxy fetches to avoid abuse

## Monitoring & Logging

- Log proxy fetch attempts and results
- Log health check results
- Track proxy success/failure rates
- Alert when pool size drops below threshold
- Log when falling back to no-proxy mode

## Testing Strategy

1. Unit tests for proxy fetcher
2. Unit tests for health checker
3. Integration tests for proxy pool manager
4. Test with real YouTube requests
5. Test failure scenarios (all proxies dead, network errors)

## Rollout Plan

1. Implement proxy fetcher (Step 1)
2. Implement health checker (Step 2)
3. Implement pool manager (Step 3)
4. Add configuration (Step 4)
5. Integrate with existing code (Step 5)
6. Test in development environment
7. Enable with `youtube_scraper_enable_free_proxies=true`
8. Monitor and adjust thresholds

## Future Enhancements

- Support for SOCKS proxies
- Proxy geolocation filtering
- Automatic proxy source discovery
- Machine learning for proxy quality prediction
- Integration with paid proxy services as fallback
