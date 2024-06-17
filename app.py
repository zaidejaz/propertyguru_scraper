import os
import asyncio
import logging
from flask import Flask, request, redirect, url_for, render_template, flash,jsonify
from werkzeug.utils import secure_filename
from pyppeteer import launch
from pyppeteer_stealth import stealth
from bs4 import BeautifulSoup
import pandas as pd
from concurrent.futures import ProcessPoolExecutor

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'supersecretkey'
app.config['MAX_CONTENT_PATH'] = 10000000

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
app.secret_key = "supersecretkey"

# Define logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/scrape", methods=["POST"])
def scrape():
    search_url = request.form.get("url")
    file = request.files.get("file")

    if not search_url:
        return jsonify({"status": "error", "message": "Property Search URL is required."})

    file_path = None
    if file and file.filename:
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(file_path)

    try:
        with ProcessPoolExecutor() as executor:
            future = executor.submit(run_scraping, search_url, file_path)
            future.result()
        return jsonify({"status": "success", "message": "Scraping and updating completed successfully!"})
    except Exception as e:
        logging.error(f"Error during scraping and updating: {e}")
        return jsonify({"status": "error", "message": "An error occurred during scraping and updating."})

def run_scraping(url, file_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    combined_listings_data = loop.run_until_complete(scrape_property(url))
    update_excel(combined_listings_data, file_path)

async def get_page_content(url):
    try:
        browser = await launch(headless=True, args=["--no-sandbox"])
        page = await browser.newPage()
        await stealth(page)
        await page.goto(url, {"waitUntil": "load", "timeout": 0})
        content = await page.content()
        await browser.close()
        return content
    except Exception as e:
        logging.error(f"Error fetching page content for URL {url}: {e}")
        return None

def parse_listings(content):
    if not content:
        return []
    soup = BeautifulSoup(content, "html.parser")
    listings = []

    for listing in soup.find_all("div", class_="listing-card"):
        link_tag = listing.find("a", class_="nav-link")
        if link_tag:
            link = link_tag["href"]
            listings.append(link)

    logging.info(f"Found {len(listings)} listings.")
    return listings

def parse_price(price_str):
    if price_str:
        return float(price_str.replace('S$', '').replace(',', '').strip())
    return None

async def extract_listing_details(url):
    content = await get_page_content(url)
    if not content:
        return None
    soup = BeautifulSoup(content, "html.parser")

    def get_text(selector):
        element = soup.select_one(selector)
        return element.get_text(strip=True) if element else None

    def get_text_from_label(label):
        element = soup.find("div", string=label)
        if element:
            next_div = element.find_next_sibling("div")
            return next_div.get_text(strip=True) if next_div else None
        return None

    def get_agent_details():
        agent_element = soup.select_one("div.agent-name-wrapper a")
        agent_description = soup.find("div", class_="agent-description")
        cea_number = None
        agency_element = soup.select_one("div.agency")
        agency = agency_element.get_text(strip=True) if agency_element else None
        if agent_description:
            cea_text = agent_description.get_text(strip=True)
            cea_number = cea_text.split("CEA: ")[1].split(" / ")[0] if "CEA: " in cea_text else None
        return (
            agent_element.get_text(strip=True) if agent_element else None,
            cea_number,
            agency
        )

    def get_labels():
        return [label.get_text(strip=True) for label in soup.select("div.labels div.label")]

    def get_amenities():
        amenities = soup.select("div.amenities div.amenity h4")
        details = {"bedrooms": None, "bathrooms": None, "land_size": None, "psf": None}
        for amenity in amenities:
            text = amenity.get_text(strip=True).lower()
            if "bed" in text:
                details["bedrooms"] = text.split(" bed")[0]
            elif "bath" in text:
                details["bathrooms"] = text.split(" bath")[0]
            elif "sqft" in text:
                details["land_size"] = text.split(" sqft")[0]
            elif "psf" in text:
                details["psf"] = text.split(" psf")[0]
        return details

    def parse_mrt_distance(mrt_text):
        if mrt_text:
            parts = mrt_text.split(" from ")
            distance_time = parts[0].strip()
            mrt_station = parts[1].strip() if len(parts) > 1 else None
            return distance_time, mrt_station
        return None, None

    agent, cea_number, agency = get_agent_details()
    labels = get_labels()
    amenities = get_amenities()
    mrt_distance, mrt_station = parse_mrt_distance(get_text("span.mrt-distance__text"))

    details = {
        "Links": [url],  # Initialize with the current URL
        "Agent Name": agent,
        "Agent Phone Number": None,  # Placeholder for future use
        "Agent Cea Number": cea_number,
        "Agency": agency,
        "No Of Agent Listing": 1,  # Initial count
        "Wow Change": 0,  # Placeholder for future use
        "Address": get_text("span.full-address__address"),
        "Listing Type": "new",  # Placeholder to be updated with logic
        "Property Type": get_text_from_label("Property Type"),
        "District": None,  # Extract from address logic
        "Asking Price": parse_price(get_text("h2.amount[data-automation-id='overview-price-txt']")),
        "Previous Price": None,  # Placeholder for future use
        "Price Change Percentage": None,  # Placeholder for future use
        "Bedrooms": amenities["bedrooms"],
        "Bathrooms": amenities["bathrooms"],
        "Land Size": amenities["land_size"],
        "Psf": amenities["psf"],
        "First Listed On": get_text_from_label("Listed On"),
        "Tenancy": None,  # Placeholder for future use
        "Mrt Distance": mrt_distance,
        "Nearest Mrt": mrt_station
    }

    # Extract district from address logic
    if details["Address"]:
        address_parts = details["Address"].split("(")
        if len(address_parts) > 1:
            details["District"] = address_parts[1].split(")")[0].strip()

    return details

async def scrape_property(search_url):
    content = await get_page_content(search_url)
    listing_links = parse_listings(content)

    listings_data = []
    for link in listing_links:
        details = await extract_listing_details(link)
        if details:
            listings_data.append(details)

    # Process listings to combine and update details as required
    combined_listings = {}
    for listing in listings_data:
        address = listing["Address"]
        if address not in combined_listings:
            combined_listings[address] = listing
        else:
            combined_listing = combined_listings[address]
            combined_listing["Links"].extend(listing["Links"])
            combined_listing["No Of Agent Listing"] += 1
            combined_listing["Wow Change"] += 1
            combined_listing["Previous Price"] = combined_listing["Asking Price"]
            combined_listing["Asking Price"] = listing["Asking Price"]
            combined_listing["Price Change Percentage"] = (
                (combined_listing["Asking Price"] - combined_listing["Previous Price"])
                / combined_listing["Previous Price"] * 100
                if combined_listing["Previous Price"]
                else "N/A"
            )

    return combined_listings

def update_excel(data, file_path):
    if file_path and os.path.exists(file_path):
        df_existing = pd.read_excel(file_path)
        df_existing = df_existing.applymap(lambda x: x if not pd.isna(x) else None)
    else:
        df_existing = pd.DataFrame(columns=data.keys())

    df_new = pd.DataFrame.from_dict(data, orient='index').reset_index(drop=True)
    
    df_combined = pd.concat([df_existing, df_new]).drop_duplicates(subset=["Address"], keep="last")
    
    df_combined.to_excel(file_path or 'scraped_data.xlsx', index=False)

def run_scraping(url, file_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    combined_listings_data = loop.run_until_complete(scrape_property(url))
    update_excel(combined_listings_data, file_path)


if __name__ == "__main__":
    if not os.path.exists(app.config["UPLOAD_FOLDER"]):
        os.makedirs(app.config["UPLOAD_FOLDER"])
    app.run(debug=True, port=5000)