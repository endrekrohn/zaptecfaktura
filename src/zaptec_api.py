from typing import Dict, Any, List
import httpx


class ZaptecAPI:
    def __init__(self, base_url: str):
        self.base_url = base_url

    async def get_installations(self, token: str) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/installation"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"PageSize": 100}

        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code != 200:
                raise Exception(f"Failed to fetch installations: {response.text}")
            return response.json().get("Data", [])

    async def get_chargehistory(
        self, token: str, installation_id: str, from_date: str, to_date: str
    ) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/chargehistory"
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "InstallationId": installation_id,
            "From": from_date,
            "To": to_date,
            "DetailLevel": 1,
            "PageSize": 100,
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code != 200:
                raise Exception(f"Failed to fetch charge history: {response.text}")
            return response.json().get("Data", [])
