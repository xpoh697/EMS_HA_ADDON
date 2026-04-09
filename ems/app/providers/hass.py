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
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the current state of a Home Assistant entity."""
        url = f"{self.base_url}/api/states/{entity_id}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as e:
                logger.error(f"Error fetching state for {entity_id}: {e}")
                return None

    async def get_all_states(self) -> List[Dict[str, Any]]:
        """Fetch all entity states for discovery."""
        url = f"{self.base_url}/api/states"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as e:
                logger.error(f"Error fetching all states: {e}")
                return []


    async def call_service(self, domain: str, service: str, service_data: Dict[str, Any]) -> bool:
        """Call a Home Assistant service."""
        url = f"{self.base_url}/api/services/{domain}/{service}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=self.headers, json=service_data)
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
