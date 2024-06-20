import os
import asyncio
import logging
from datetime import datetime
from flask import Flask, request, render_template, jsonify, send_file, url_for
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
app.config['DOWNLOAD_FOLDER'] = 'downloads'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
        return jsonify({
            "status": "success",
            "message": "Scraping and updating completed successfully!",
            "download_url": url_for('download_file', filename='scraped_data.xlsx')
        })
    except Exception as e:
        logging.error(f"Error during scraping and updating: {e}")
        return jsonify({"status": "error", "message": "An error occurred during scraping and updating."})

@app.route("/download/<filename>")
def download_file(filename):
    return send_file(os.path.join(app.config['DOWNLOAD_FOLDER'], filename), as_attachment=True)

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
            # Split by '(' to separate distance and time
            parts = mrt_text.split('(')

            # Extract time
            mrt_time = parts[0].strip()

            # Extract distance from the second part
            if len(parts) > 1:
                mrt_distance = parts[1].replace(')', '').strip()
            else:
                mrt_distance = None

            return mrt_time, mrt_distance

        return None, None


    agent, cea_number, agency = get_agent_details()
    labels = get_labels()
    amenities = get_amenities()
    distance_time, mrt_station = parse_mrt_distance(get_text("span.mrt-distance__text"))
    if distance_time:
        start_index = distance_time.find('(')
        end_index = distance_time.find(')')
        
        if start_index != -1 and end_index != -1:
            mrt_time = distance_time[:start_index].strip()
            mrt_distance = distance_time[start_index + 1:end_index].strip()
            return mrt_time, mrt_distance

    details = {
        "Address": get_text("span.full-address__address"),
        "Listing Type": "new",
        "District": None,
        "Links": [url],
        "Asking Price": parse_price(get_text("h2.amount[data-automation-id='overview-price-txt']")),
        "Previous Price": None,
        "Wow Change": None,
        "Size / Strata Size (sqft)": amenities["land_size"],
        "$PSF": amenities["psf"],
        "Land Gross Floor Area (sqft)": get_text_from_label("Floor Size"),
        "Property Type": get_text_from_label("Property Type"),
        "Tenancy": get_text_from_label("Currently Tenanted"),
        "Bedrooms": amenities["bedrooms"],
        "Bathrooms": amenities["bathrooms"],
        "Days in Market": None, 
        "First Listed On": get_text_from_label("Listed On"),
        "Mrt Distance": mrt_distance,
        "Mrt Time": mrt_time,
        "Nearest Mrt": mrt_station,
        "No Of Agent Listing": 1,
        "Listing Agents Change": 0,
        "Agent's CEA Number": cea_number,
        "Agent's Name": agent,
        "Agent's Phone Number": None,
        "Agency": agency,
    }

    if details["Address"]:
        address_parts = details["Address"].split("(")
        if len(address_parts) > 1:
            district = address_parts[1].split(")")[0].strip()
            details["District"] = district.replace("D", "")

    today = datetime.today().date()
    listed_on = datetime.strptime(details["First Listed On"], '%d %b %Y').date()
    details["Days in Market"] = (today - listed_on).days

    return details

async def scrape_property(search_url):
    content = await get_page_content(search_url)
    listing_links = parse_listings(content)

    listings_data = []
    for link in listing_links:
        details = await extract_listing_details(link)
        if details:
            listings_data.append(details)

    combined_listings = {}
    for listing in listings_data:
        address = listing["Address"]
        if address not in combined_listings:
            combined_listings[address] = listing
        else:
            combined_listing = combined_listings[address]
            combined_listing["Links"].extend(listing["Links"])
            combined_listing["No Of Agent Listing"] = len(listing["Links"])
            combined_listing["Wow Change"] = listing["Asking Price"] - combined_listing["Asking Price"]
            combined_listing["Previous Price"] = combined_listing["Asking Price"]
            combined_listing["Asking Price"] = listing["Asking Price"]

    return combined_listings

def update_excel(data, file_path):
    columns = ["Address", "Listing Type", "District", "Links", "Asking Price", "Previous Price",
               "Wow Change", "Size / Strata Size (sqft)", "$PSF", "Land Gross Floor Area (sqft)",
               "Property Type", "Tenancy", "Bedrooms", "Bathrooms", "Days in Market", "First Listed On",
               "Mrt Distance", "Mrt Time", "Nearest Mrt", "No Of Agent Listing", "Listing Agents Change",
               "Agent's CEA Number", "Agent's Name", "Agent's Phone Number", "Agency"]

    df_new = pd.DataFrame.from_dict(data, orient='index').reset_index(drop=True)

    if file_path and os.path.exists(file_path):
        df_existing = pd.read_excel(file_path)
        df_existing = df_existing.applymap(lambda x: x if not pd.isna(x) else None)
    else:
        df_existing = pd.DataFrame(columns=columns)

    df_combined = pd.concat([df_existing, df_new]).drop_duplicates(subset=["Address"], keep="last")

    # Update status: Existing or Expired
    for index, row in df_combined.iterrows():
        address = row["Address"]
        existing_row = df_existing[df_existing["Address"] == address]
        if not existing_row.empty:
            # Update status to "Existing"
            df_combined.at[index, "Listing Type"] = "Existing"
            # Check if links are expired
            existing_links = set(existing_row["Links"].values[0]) if not existing_row["Links"].isnull().values.any() else set()
            current_links = set(row["Links"])
            if existing_links.difference(current_links):
                df_combined.at[index, "Listing Type"] = "Expired"
            # Keep the original First Listed On date
            df_combined.at[index, "First Listed On"] = existing_row["First Listed On"].values[0]
            # Calculate Days in Market
            listed_date = datetime.strptime(existing_row["First Listed On"].values[0], '%Y-%m-%d')
            days_in_market = (datetime.now() - listed_date).days
            df_combined.at[index, "Days in Market"] = days_in_market
        else:
            # Calculate Days in Market for new listings
            listed_date = datetime.strptime(row["First Listed On"], '%d %b %Y')
            days_in_market = (datetime.now() - listed_date).days
            df_combined.at[index, "Days in Market"] = days_in_market

    output_path = os.path.join(app.config['DOWNLOAD_FOLDER'], 'scraped_data.xlsx')
    df_combined.to_excel(output_path, index=False)

if __name__ == "__main__":
    if not os.path.exists(app.config["UPLOAD_FOLDER"]):
        os.makedirs(app.config["UPLOAD_FOLDER"])
    if not os.path.exists(app.config["DOWNLOAD_FOLDER"]):
        os.makedirs(app.config["DOWNLOAD_FOLDER"])
    app.run(debug=True, port=5002)
