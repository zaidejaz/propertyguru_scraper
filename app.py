import os
import asyncio
import logging
import time
from datetime import datetime
from flask import Flask, request, render_template, jsonify, send_file, url_for
from werkzeug.utils import secure_filename
from pyppeteer import launch
from pyppeteer_stealth import stealth
from bs4 import BeautifulSoup
import pandas as pd
from concurrent.futures import ProcessPoolExecutor
import os

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'supersecretkey'
app.config['MAX_CON TENT_PATH'] = 10000000
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

async def login(page):
    logging.info("Logging in...")
    await page.goto('https://www.propertyguru.com.sg/', {"waitUntil": "load", "timeout": 0})
    logging.info("Navigated to PropertyGuru homepage.")
    await page.click('button[data-automation-id="navigation-login"]')
    logging.info("Login Button Clicked.....")
    # Wait for the email field to be available before typing
    await page.waitForSelector('input[data-automation-id="email-fld"]')
    await page.type('input[data-automation-id="email-fld"]', os.environ.get("EMAIL"))
    logging.info("Email Entered....")
    await page.click('button[data-automation-id="continue-btn"]')
    logging.info("Continue")  
    # Wait for the password field to be available before typing
    await page.waitForSelector('input[data-automation-id="password-fld"]')
    await page.type('input[data-automation-id="password-fld"]', os.environ.get("PASSWORD"))
    logging.info("Password Entered.....")
    await page.click('button[data-automation-id="submit-btn"]') 
    logging("Hello World!")
    logging.info("Login successful.")


async def get_page_content(url, page):
    try:
        await page.goto(url, {"waitUntil": "load", "timeout": 0})
        content = await page.content()
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

async def extract_listing_details(url, page):
    content = await get_page_content(url, page)
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
            start_index = mrt_text.find('(')
            end_index = mrt_text.find(')')
            if start_index != -1 and end_index != -1:
                mrt_time = mrt_text[:start_index].strip()
                mrt_distance = mrt_text[start_index + 1:end_index].strip()
                return mrt_time, mrt_distance
        return None, None

    def get_agent_phone_number():
        whatsapp_link = soup.select_one('a[data-automation-id="enquiry-widget-whatsapp-btn"]')
        if whatsapp_link:
            href = whatsapp_link['href']
            phone_number = href.split('https://wa.me/')[1].split('?')[0]
            return phone_number
        return None

    agent, cea_number, agency = get_agent_details()
    labels = get_labels()
    amenities = get_amenities()
    distance_time, mrt_station = parse_mrt_distance(get_text("span.mrt-distance__text"))
    mrt_time, mrt_distance = distance_time, mrt_station

    details = {
        "Address": get_text("span.full-address__address"),
        "Listing Type": "new",
        "District": None,
        "Links": [url],
        "Asking Price": parse_price(get_text("h2.amount[data-automation-id='overview-price-txt']")),
        "Previous Price": None,
        "Wow Change": None,
        "Size / Strata Size (sqft)": amenities["land_size"],
        "$PSF": amenities["psf"].replace("s", ""),
        "Land Gross Floor Area (sqft)": get_text_from_label("Floor Size"),
        "Property Type": get_text_from_label("Property Type"),
        "Tenancy": get_text_from_label("Currently Tenanted"),
        "Bedrooms": amenities["bedrooms"],
        "Bathrooms": amenities["bathrooms"],
        "Days in Market": None, 
        "First Listed On": get_text_from_label("Listed On"),
        "Mrt Distance": mrt_distance,
        "Nearest Mrt": mrt_time,
        "No Of Agent Listing": 1,
        "Listing Agents Change": 0,
        "Agent's CEA Number": cea_number,
        "Agent's Name": agent,
        "Agent's Phone Number": get_agent_phone_number(),
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
    browser_options = {
        'headless': True,
        'args': [
            '--start-maximized',
            '--window-size=1920,1080' 
        ]
    }
    browser = await launch(browser_options)
    page = await browser.newPage()
    await page.setViewport({
        'width': 1920,
        'height': 1080,
    })
    await stealth(page)
    await login(page)
    content = await get_page_content(search_url, page)
    listing_links = parse_listings(content)

    listings_data = []
    for link in listing_links:
        details = await extract_listing_details(link, page)
        if details:
            listings_data.append(details)

    combined_listings_df = pd.DataFrame(listings_data)
    output_filename = os.path.join(app.config['DOWNLOAD_FOLDER'], "scraped_data.xlsx")
    combined_listings_df.to_excel(output_filename, index=False)

    await browser.close()
    return combined_listings_df

def update_excel(scraped_data, file_path):
    if file_path:
        existing_df = pd.read_excel(file_path)
        for index, row in existing_df.iterrows():
            listing_url = row["Links"]
            matched_row = scraped_data.loc[scraped_data["Links"] == listing_url]
            if not matched_row.empty:
                for column in scraped_data.columns:
                    if column in row and not pd.isnull(matched_row.iloc[0][column]):
                        existing_df.at[index, column] = matched_row.iloc[0][column]
        updated_filename = os.path.join(app.config['DOWNLOAD_FOLDER'], "updated_" + os.path.basename(file_path))
        existing_df.to_excel(updated_filename, index=False)
    else:
        scraped_data.to_excel(os.path.join(app.config['DOWNLOAD_FOLDER'], "scraped_data.xlsx"), index=False)

if __name__ == "__main__":
    app.run(debug=True)
