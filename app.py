import streamlit as st
import folium
from streamlit_folium import st_folium

# Page configuration
st.set_page_config(
    page_title="Global Ship Tracker",
    page_icon="🚢",
    layout="wide"
)

# Title
st.title("🌍 Global Ship Tracker")

st.write(
    "Live maritime traffic visualization using AIS data"
)

# Create world map
m = folium.Map(
    location=[20, 0],
    zoom_start=2,
    tiles="CartoDB positron"
)

# Sample ship data
ships = [
    {
        "name": "Demo Cargo Ship",
        "lat": 19.0760,
        "lon": 72.8777,
        "speed": 14.2,
        "type": "Cargo"
    },
    {
        "name": "Demo Container Ship",
        "lat": 1.2903,
        "lon": 103.8519,
        "speed": 18.5,
        "type": "Container"
    },
    {
        "name": "Demo Tanker",
        "lat": 31.2304,
        "lon": 121.4737,
        "speed": 11.8,
        "type": "Tanker"
    }
]

# Add ships to map
for ship in ships:

    popup = f"""
    <b>{ship['name']}</b><br>
    Type: {ship['type']}<br>
    Speed: {ship['speed']} knots<br>
    Latitude: {ship['lat']}<br>
    Longitude: {ship['lon']}
    """

    folium.Marker(
        location=[ship["lat"], ship["lon"]],
        popup=popup,
        tooltip=ship["name"],
        icon=folium.Icon(
            icon="ship",
            prefix="fa"
        )
    ).add_to(m)
    
# Display map
st_folium(
    m,
    width=None,
    height=650
)