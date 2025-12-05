#!/usr/bin/env python3
import requests
import json
import time
from proxy_manager import ProxyManager

def fetch_free_proxies():
    """Fetch a list of free proxies from various sources"""
    proxies = []
    
    # Source 1: ProxyScrape API
    try:
        response = requests.get('https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all')
        if response.status_code == 200:
            proxy_list = response.text.strip().split('\n')
            for proxy in proxy_list:
                if ':' in proxy:
                    ip, port = proxy.strip().split(':')
                    proxies.append({"https": f"https://{ip}:{port}"})
    except Exception as e:
        print(f"Error fetching from ProxyScrape: {str(e)}")
    
    # Source 2: Free-Proxy-List.net
    try:
        response = requests.get('https://free-proxy-list.net/')
        if response.status_code == 200:
            # Simple parsing - in a real implementation, you'd use BeautifulSoup
            lines = response.text.split('\n')
            for line in lines:
                if ':' in line and '.' in line:
                    parts = line.strip().split()
                    for part in parts:
                        if ':' in part and '.' in part:
                            ip, port = part.split(':')
                            proxies.append({"https": f"https://{ip}:{port}"})
                            break
    except Exception as e:
        print(f"Error fetching from Free-Proxy-List: {str(e)}")
    
    return proxies

def main():
    print("Finding and validating proxies...")
    proxy_manager = ProxyManager()
    
    # Fetch new proxies
    new_proxies = fetch_free_proxies()
    print(f"Found {len(new_proxies)} potential proxies")
    
    # Test and add working proxies
    working_count = 0
    for proxy in new_proxies:
        print(f"Testing proxy: {proxy['https']}")
        if proxy_manager.add_proxy(proxy):
            working_count += 1
            print(f"✅ Added working proxy: {proxy['https']}")
        else:
            print(f"❌ Proxy failed: {proxy['https']}")
        time.sleep(1)  # Avoid rate limiting
    
    print(f"\nAdded {working_count} working proxies out of {len(new_proxies)} tested")
    print(f"Total working proxies: {len(proxy_manager.proxies)}")

if __name__ == "__main__":
    main() 