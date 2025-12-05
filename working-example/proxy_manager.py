#!/usr/bin/env python3
import requests
import json
import time
import random
from typing import Dict, List, Optional
import urllib3

# Disable SSL warnings for proxy testing
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class ProxyManager:
    def __init__(self, proxy_list_file: str = "proxies.json"):
        self.proxy_list_file = proxy_list_file
        self.proxies: List[Dict[str, str]] = []
        self.last_validated: float = 0
        self.validation_interval = 3600  # Validate proxies every hour
        self.load_proxies()
    
    def load_proxies(self) -> None:
        """Load proxies from the JSON file"""
        try:
            with open(self.proxy_list_file, 'r') as f:
                self.proxies = json.load(f)
        except FileNotFoundError:
            # Initialize with empty list if file doesn't exist
            self.proxies = []
            self.save_proxies()
    
    def save_proxies(self) -> None:
        """Save proxies to the JSON file"""
        with open(self.proxy_list_file, 'w') as f:
            json.dump(self.proxies, f, indent=2)
    
    def validate_proxy(self, proxy: Dict[str, str]) -> bool:
        """Test if a proxy is working with multiple methods"""
        # Try with verify=False to bypass SSL verification issues
        try:
            response = requests.get(
                'https://www.youtube.com',
                proxies=proxy,
                timeout=5,
                verify=False
            )
            return response.status_code == 200
        except Exception as e:
            # Try with HTTP instead of HTTPS
            try:
                http_proxy = {"http": proxy["https"].replace("https://", "http://")}
                response = requests.get(
                    'http://www.youtube.com',
                    proxies=http_proxy,
                    timeout=5,
                    verify=False
                )
                return response.status_code == 200
            except Exception as e2:
                print(f"Proxy validation error: {str(e2)}")
                return False
    
    def validate_all_proxies(self) -> None:
        """Validate all proxies and remove non-working ones"""
        current_time = time.time()
        if current_time - self.last_validated < self.validation_interval:
            return
        
        working_proxies = []
        for proxy in self.proxies:
            if self.validate_proxy(proxy):
                working_proxies.append(proxy)
        
        self.proxies = working_proxies
        self.save_proxies()
        self.last_validated = current_time
    
    def add_proxy(self, proxy: Dict[str, str]) -> bool:
        """Add a new proxy to the list if it's working"""
        if self.validate_proxy(proxy):
            if proxy not in self.proxies:
                self.proxies.append(proxy)
                self.save_proxies()
            return True
        return False
    
    def get_working_proxy(self) -> Optional[Dict[str, str]]:
        """Get a working proxy from the list"""
        self.validate_all_proxies()
        if not self.proxies:
            return None
        
        # Return a random proxy from the working ones
        return random.choice(self.proxies)
    
    def add_premium_proxies(self) -> None:
        """Add premium proxies from a service (placeholder)"""
        # This is a placeholder for adding premium proxies
        # In a real implementation, you would connect to a paid proxy service API
        premium_proxies = [
            # Example premium proxies (replace with actual ones)
            # {"https": "https://username:password@proxy1.example.com:8080"},
            # {"https": "https://username:password@proxy2.example.com:8080"},
        ]
        
        for proxy in premium_proxies:
            self.add_proxy(proxy)

if __name__ == "__main__":
    # Test the proxy manager
    manager = ProxyManager()
    print(f"Loaded {len(manager.proxies)} proxies")
    print("Validating proxies...")
    manager.validate_all_proxies()
    print(f"Found {len(manager.proxies)} working proxies") 