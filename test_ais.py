import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("pk_RIZzd783uUu5HB2spYafhtpjGZNxnULwSWRxql6EZK6_0eEWRw")

url = "https://api.pelyr.com/v1/vessels"

params = {
    "bbox": "-180,-85,180,85",
    "max": 100
}

headers = {
    "Authorization": f"Bearer {API_KEY}"
}

response = requests.get(
    url,
    params=params,
    headers=headers
)

print("Status:", response.status_code)

print(response.text[:5000])