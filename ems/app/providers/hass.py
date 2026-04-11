import httpx
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

class HomeAssistantClient:
    """
    A unified client for interacting with Home Assistant REST API.
    Designed for reuse across multiple services.
    Supports multi-strategy probing to find a working connection.
    """
    def __init__(self, base_url: str, token: str):
        self.primary_base_url = base_url.rstrip("/")
        self.token = token
        self.current_base_url = self.primary_base_url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=10.0)
        self.auth_failed = False
        self.verified = False
        
        # Masked diagnostic log
        token_len = len(token)
        if token == "REPLACE_ME":
            token_status = "REPLACE_ME"
        elif token_len > 8:
            token_status = f"{token[:4]}...{token[-4:]} (len: {token_len})"
        else:
            token_status = f"Detected (len: {token_len})"
            
        logger.info(f"HomeAssistantClient initialized. Base URL: {self.primary_base_url}, Token: {token_status}")

    async def test_connection(self):
        """
        Probe multiple endpoints and header combinations to find a working strategy.
        """
        if self.token == "REPLACE_ME":
            logger.warning("No token provided. Skipping connection test.")
            return False

        candidates = [
            # 1. Standard Proxy + Bearer
            {"url": f"{self.primary_base_url}/states", "headers": {"Authorization": f"Bearer {self.token}"}, "name": "Proxy + Bearer"},
            # 2. Standard Proxy + X-Supervisor-Token
            {"url": f"{self.primary_base_url}/states", "headers": {"X-Supervisor-Token": self.token}, "name": "Proxy + X-Supervisor-Token"},
            # 3. Direct Core + Bearer
            {"url": "http://homeassistant:8123/api/states", "headers": {"Authorization": f"Bearer {self.token}"}, "name": "Direct Core + Bearer"},
            # 4. Direct Core IP + Bearer (standard Docker host)
            {"url": "http://172.30.32.1:8123/api/states", "headers": {"Authorization": f"Bearer {self.token}"}, "name": "Direct IP + Bearer"},
        ]

        logger.info("--- HA Connection Probing Started ---")
        best_strategy = None

        for c in candidates:
            try:
                logger.info(f"Probing {c['name']} at {c['url']}...")
                resp = await self.client.get(c["url"], headers=c["headers"])
                logger.info(f"Result {c['name']}: HTTP {resp.status_code}")
                
                if resp.status_code == 200:
                    logger.info(f"!!! SUCCESS: Found working strategy: {c['name']}")
                    best_strategy = c
                    break
                elif resp.status_code == 401:
                    logger.debug(f"Auth failed for {c['name']}")
            except Exception as e:
                logger.debug(f"Failed to reach {c['name']}: {e}")

        if best_strategy:
            # Commit to the working strategy
            self.current_base_url = best_strategy["url"].replace("/states", "")
            self.headers = {**best_strategy["headers"], "Content-Type": "application/json"}
            self.verified = True
            logger.info(f"Target URL committed: {self.current_base_url}")
            return True
        else:
            logger.error("!!! ALL CONNECTION STRATEGIES FAILED !!!")
            logger.error("Please create a 'Long-Lived Access Token' in HA (Profile -> Security).")
            logger.error("Paste it into the addon options if possible, or wait for next fix.")
            return False

    async def close(self):
        """Close the underlying HTTP client."""
        await self.client.aclose()

    async def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the current state of a Home Assistant entity."""
        if self.auth_failed:
            return None

        url = f"{self.current_base_url}/states/{entity_id}"
        try:
            response = await self.client.get(url, headers=self.headers)
            if response.status_code == 401:
                self._handle_auth_error()
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching state for {entity_id}: {e}")
            return None

    async def get_all_states(self) -> List[Dict[str, Any]]:
        """Fetch all entity states for discovery."""
        if self.auth_failed:
            return []

        url = f"{self.current_base_url}/states"
        try:
            response = await self.client.get(url, headers=self.headers)
            if response.status_code == 401:
                self._handle_auth_error()
                return []
            response.raise_for_status()
            data = response.json()
            return data
        except httpx.HTTPError as e:
            logger.error(f"Error fetching all states from {url}: {e}")
            return []

    def _handle_auth_error(self):
        """Handle 401 Unauthorized errors by logging and disabling further calls."""
        if not self.auth_failed:
            self.auth_failed = True
            logger.error("!!! CRITICAL: 401 Unauthorized during operation at %s", self.current_base_url)
            logger.error("Further HA API calls suspended.")

    async def call_service(self, domain: str, service: str, service_data: Dict[str, Any]) -> bool:
        """Call a Home Assistant service."""
        if self.auth_failed:
            return False

        url = f"{self.current_base_url}/services/{domain}/{service}"
        try:
            response = await self.client.post(url, headers=self.headers, json=service_data)
            if response.status_code == 401:
                self._handle_auth_error()
                return False
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Error calling service {domain}.{service}: {e}")
            return False

    async def get_config(self) -> Optional[Dict[str, Any]]:
        """Fetch Home Assistant configuration."""
        if self.auth_failed:
            return None
        url = f"{self.current_base_url}/config"
        try:
            response = await self.client.get(url, headers=self.headers)
            if response.status_code == 401:
                self._handle_auth_error()
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching HA config: {e}")
            return None

    async def turn_on(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_on", {"entity_id": entity_id})

    async def turn_off(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_off", {"entity_id": entity_id})
