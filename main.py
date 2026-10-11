import logging
import re
import time
import urllib.parse
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scraper")

app = FastAPI(title="Lead Generation Scraper API")

class ScrapeRequest(BaseModel):
    query: str
    limit: int = 15

@app.get("/")
def read_root():
    return {
        "status": "online",
        "message": "Lead Generation Scraper API is running. Send POST to /scrape with JSON {'query': '...', 'limit': 15}"
    }

def clean_phone(phone_str: str) -> str:
    if not phone_str:
        return None
    cleaned = re.sub(r'[^\d+]', '', phone_str)
    return phone_str.strip()

def extract_details_from_card(card, card_text: str):
    # 1. Name
    name = None
    try:
        name_elem = card.locator('.qBF1Pd, .fontHeadlineSmall')
        if name_elem.count() > 0:
            name = name_elem.first.inner_text().strip()
        else:
            link_elem = card.locator('a.hfpxzc, a[aria-label]')
            if link_elem.count() > 0:
                name = link_elem.first.get_attribute('aria-label')
    except Exception:
        pass

    # 2. Rating & Reviews Count
    rating = None
    reviews_count = None
    try:
        rating_elem = card.locator('span.MW4etd')
        if rating_elem.count() > 0:
            rating = rating_elem.first.inner_text().strip()
        else:
            # Fallback to aria-label or regex
            star_elem = card.locator('span[aria-label*="star" i], span[aria-label*="stars" i]')
            if star_elem.count() > 0:
                aria = star_elem.first.get_attribute('aria-label')
                m = re.search(r'([1-5]\.\d)', aria)
                if m:
                    rating = m.group(1)

        if not rating:
            m = re.search(r'\b([1-5]\.\d)\b', card_text)
            if m:
                rating = m.group(1)
    except Exception:
        pass

    try:
        reviews_elem = card.locator('span.UY7F9')
        if reviews_elem.count() > 0:
            raw_rev = reviews_elem.first.inner_text().strip()
            reviews_count = re.sub(r'[^\d]', '', raw_rev) or raw_rev
        else:
            m = re.search(r'\(([0-9,]+)\)', card_text)
            if m:
                reviews_count = m.group(1).replace(',', '')
    except Exception:
        pass

    # 3. Website
    website = None
    try:
        web_elem = card.locator('a[aria-label*="Website" i], a[data-value="Website" i], a[href*="http"]:has-text("Website")')
        if web_elem.count() > 0:
            website = web_elem.first.get_attribute('href')
    except Exception:
        pass

    # 4. Address, Phone, Category parsing from lines and W4Efsd blocks
    phone = None
    address = None
    category = None

    # Phone number extraction (handles standard Indian mobile/landline 09xxxx xxxxx, +91, etc.)
    phone_match = re.search(r'\b(?:0|\+?91[\-\s]?)?[6-9]\d{4}[\s\-]?\d{5}\b|\b0\d{2,4}[\s\-]?\d{6,8}\b|\b09\d{4}\s*\d{5}\b|\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,5}\)?[-.\s]?\d{3,5}[-.\s]?\d{3,5}\b', card_text)
    if phone_match:
        phone = phone_match.group(0).strip()

    # Extract lines / blocks to find Category and Address
    try:
        w4_blocks = card.locator('div.W4Efsd').all()
        block_texts = [b.inner_text().strip() for b in w4_blocks if b.inner_text().strip()]
        
        # Split blocks by newlines and dots
        lines = [line.strip() for line in card_text.split('\n') if line.strip()]
        
        # Ignored words for address
        ignore_keywords = ["open", "closed", "opens", "closes", "hours", "website", "directions", "share", "save", "on-site", "online classes"]

        candidate_addresses = []
        for line in lines:
            if name and line == name:
                continue
            if line.startswith(str(rating or "___")):
                # Check for category after dot
                parts = [p.strip() for p in line.split('·')]
                if len(parts) > 1 and not category:
                    category = parts[-1]
                continue

            # Check if this line contains address/phone
            parts = [p.strip() for p in line.split('·')]
            for part in parts:
                lower = part.lower()
                if any(kw in lower for kw in ignore_keywords):
                    continue
                if phone and phone in part:
                    # Remove phone from the address part
                    cleaned_part = part.replace(phone, '').strip(' ·,-')
                    if cleaned_part and len(cleaned_part) > 3:
                        candidate_addresses.append(cleaned_part)
                    continue
                if re.search(r'^[1-5]\.\d', part) or re.search(r'^\([0-9,]+\)$', part):
                    continue
                if len(part) > 4 and not category and len(parts) == 1 and not any(c.isdigit() for c in part):
                    category = part
                elif len(part) > 3:
                    candidate_addresses.append(part)

        if candidate_addresses:
            # Pick the longest or best matching address snippet
            address = candidate_addresses[0]
            if len(candidate_addresses) > 1 and len(candidate_addresses[1]) > len(address):
                address = candidate_addresses[1]

    except Exception as e:
        logger.debug(f"Details extraction error: {e}")

    return {
        "name": name,
        "category": category,
        "phone": phone,
        "address": address,
        "rating": rating,
        "reviews_count": reviews_count,
        "website": website
    }

@app.post("/scrape")
def scrape_maps(req: ScrapeRequest):
    results = []
    encoded_query = urllib.parse.quote_plus(req.query.strip())
    url = f"https://www.google.com/maps/search/{encoded_query}"
    logger.info(f"Received scraping request: query='{req.query}', limit={req.limit}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="en-US"
            )
            page = context.new_page()

            logger.info(f"Navigating to {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Dismiss Google consent dialogs if present
            try:
                consent_button = page.locator('button[aria-label*="Accept all"], form[action*="consent"] button')
                if consent_button.count() > 0:
                    consent_button.first.click(timeout=3000)
            except Exception:
                pass

            # Wait for search results feed or a single place container
            try:
                page.wait_for_selector('div[role="feed"], div[role="article"], div.Nv2PK, h1.DUwDvf', timeout=15000)
            except Exception:
                logger.warning("Selector wait timed out, attempting to parse current page content...")

            time.sleep(2)

            # Check if this is a single place page redirect
            single_heading = page.locator('h1.DUwDvf')
            if single_heading.count() > 0 and page.locator('div[role="feed"]').count() == 0:
                name = single_heading.first.inner_text().strip()
                full_text = page.inner_text("body")
                rating_match = re.search(r'\b([1-5]\.\d)\b', full_text)
                phone_match = re.search(r'\b(?:0|\+?91[\-\s]?)?[6-9]\d{4}[\s\-]?\d{5}\b|\b0\d{2,4}[\s\-]?\d{6,8}\b|\b09\d{4}\s*\d{5}\b', full_text)

                phone_btn = page.locator('button[data-item-id^="phone:tel:"]')
                website_btn = page.locator('a[data-item-id="authority"]')
                address_btn = page.locator('button[data-item-id="address"]')

                phone_val = phone_btn.first.inner_text().strip() if phone_btn.count() > 0 else (phone_match.group(0).strip() if phone_match else None)
                website_val = website_btn.first.get_attribute("href") if website_btn.count() > 0 else None
                address_val = address_btn.first.inner_text().strip() if address_btn.count() > 0 else None

                results.append({
                    "name": name,
                    "category": None,
                    "phone": phone_val,
                    "address": address_val,
                    "rating": rating_match.group(1) if rating_match else None,
                    "reviews_count": None,
                    "website": website_val
                })
            else:
                # Scroll feed to load items up to the requested limit
                scrollable = page.locator('div[role="feed"]')
                if scrollable.count() > 0:
                    scroll_count = max(3, min(req.limit // 3, 10))
                    for _ in range(scroll_count):
                        scrollable.evaluate("el => el.scrollBy(0, 1500)")
                        time.sleep(1.2)

                # Locate result cards
                cards = page.locator('div[role="article"], div.Nv2PK').all()
                logger.info(f"Found {len(cards)} cards on page")

                seen_names = set()
                for card in cards:
                    if len(results) >= req.limit:
                        break
                    try:
                        card_text = card.inner_text()
                        data = extract_details_from_card(card, card_text)

                        # Har card par click karke detail panel se phone aur website nikalna
                        try:
                            link_elem = card.locator('a.hfpxzc')
                            if link_elem.count() > 0:
                                link_elem.first.click(timeout=3000)
                            else:
                                card.click(timeout=3000)

                            page.wait_for_timeout(1500)

                            # Detail panel se direct phone selector
                            phone_btn = page.locator('button[data-item-id^="phone:tel:"]')
                            if phone_btn.count() > 0:
                                detailed_phone = phone_btn.first.inner_text().strip()
                                if detailed_phone:
                                    data["phone"] = detailed_phone

                            # Direct website selector
                            website_btn = page.locator('a[data-item-id="authority"]')
                            if website_btn.count() > 0:
                                detailed_website = website_btn.first.get_attribute("href")
                                if detailed_website:
                                    data["website"] = detailed_website

                            # Detail panel se direct address selector (agar missing ho ya chhota ho)
                            address_btn = page.locator('button[data-item-id="address"]')
                            if address_btn.count() > 0 and (not data.get("address") or len(data.get("address", "")) < 5):
                                detailed_address = address_btn.first.inner_text().strip()
                                if detailed_address:
                                    data["address"] = detailed_address
                        except Exception as detail_err:
                            logger.debug(f"Error fetching detail panel info: {detail_err}")

                        if data["name"] and data["name"] not in seen_names:
                            seen_names.add(data["name"])
                            results.append(data)
                    except Exception as card_err:
                        logger.debug(f"Error parsing card: {card_err}")
                        continue

            browser.close()

        logger.info(f"Scraped {len(results)} results successfully.")
        return {"status": "success", "count": len(results), "data": results}
    except Exception as e:
        logger.error(f"Scraping failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)