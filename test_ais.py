import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("PELYR_API_KEY")

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
    headers=headers,
    timeout=0
)

print("Status:", response.status_code)

if response.status_code == 200:
    data = response.json()

    print("Number of vessels:", data.get("count"))

    for vessel in data.get("vessels", [])[:10]:

        position = vessel.get("position", {})
        static = vessel.get("static", {})

        print("\n--------------------")
        print("Name:", static.get("name"))
        print("MMSI:", vessel.get("mmsi"))
        print("IMO:", static.get("imo"))
        print("Latitude:", position.get("lat"))
        print("Longitude:", position.get("lon"))
        print("Speed:", position.get("sog"))
        print("Course:", position.get("cog"))
        print("Destination:", static.get("dest"))

else:
    print("Error:")
    print(response.text)