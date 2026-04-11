import httpx
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

class HomeAssistantClient:
    """
    A unified client for interacting with Home Assistant REST API.
    Designed for reuse across multiple services.
    """
    def __init__(self, base_url: str, token: str):
        # Ensure base_url ends with /api for consistency if it's the supervisor proxy
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Supervisor-Token": token,  # Redundant header for some proxy versions
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=15.0
        )
        self.auth_failed = False
        
        # Diagnostic log (no sensitive data)
        token_len = len(token)
        token_status = "REPLACE_ME" if token == "REPLACE_ME" else f"Detected (len: {token_len})"
        
        if token != "REPLACE_ME" and token_len < 20:
            logger.warning(f"!!! WARNING: Token length is very short ({token_len}). This might not be a valid JWT.")
            
        logger.info(f"HomeAssistantClient initialized. Base URL: {self.base_url}, Token: {token_status}")

    async def close(self):
        """Close the underlying HTTP client."""
        await self.client.aclose()

    async def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the current state of a Home Assistant entity."""
        if self.auth_failed:
            return None

        url = f"/states/{entity_id}"
        try:
            response = await self.client.get(url)
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

        url = "/states"
        logger.info(f"Discovery: fetching all states from {self.base_url}{url}")
        try:
            response = await self.client.get(url)
            if response.status_code == 401:
                self._handle_auth_error()
                return []
            response.raise_for_status()
            data = response.json()
            logger.info(f"Discovery: found {len(data)} entities")
            return data
        except httpx.HTTPError as e:
            logger.error(f"Error fetching all states from {url}: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response body: {e.response.text}")
            return []

    def _handle_auth_error(self):
        """Handle 401 Unauthorized errors by logging and disabling further calls."""
        if not self.auth_failed:
            self.auth_failed = True
            logger.error("!!! CRITICAL: 401 Unauthorized from Home Assistant. URL: %s", self.base_url)
            logger.error("Please check your SUPERVISOR_TOKEN permissions (Role should be Admin).")
            logger.error("Further HA API calls will be suspended to avoid log flooding.")

    async def call_service(self, domain: str, service: str, service_data: Dict[str, Any]) -> bool:
        """Call a Home Assistant service."""
        if self.auth_failed:
            return False

        url = f"/services/{domain}/{service}"
        try:
            response = await self.client.post(url, json=service_data)
            if response.status_code == 401:
                self._handle_auth_error()
                return False
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Error calling service {domain}.{service}: {e}")
            return False

    async def turn_on(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_on", {"entity_id": entity_id})

    async def turn_off(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_off", {"entity_id": entity_id})
