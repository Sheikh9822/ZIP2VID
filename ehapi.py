# ehapi.py
import asyncio
import aiohttp
import urllib.parse
from bs4 import BeautifulSoup
import re
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class EHentaiScraper:
    def __init__(self, user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0',
                 session=None):
        self.user_agent = user_agent
        self.session = session
        self.semaphore = asyncio.Semaphore(5)

    async def close(self):
        if self.session:
            await self.session.close()

    async def fetch_html(self, url):
        retries = 3
        for attempt in range(retries):
            try:
                async with self.semaphore:
                    async with self.session.get(url, timeout=10) as response:
                        if response.status == 429:
                            retry_after = int(response.headers.get('Retry-After', 1))
                            logging.warning(f"Rate limited. Retrying after {retry_after} seconds.")
                            await asyncio.sleep(retry_after)
                            continue
                        if not response.ok:
                            raise Exception(f"Failed to fetch {url}: {response.status} {response.reason}")
                        return await response.text()
            except aiohttp.ClientError as e:
                logging.error(f"aiohttp error fetching {url}: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logging.error(f"Failed to fetch {url} after multiple retries.")
                    return None
            except asyncio.TimeoutError:
                logging.warning(f"Timeout fetching {url}. Retrying...")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logging.error(f"Failed to fetch {url} after multiple retries due to timeout.")
                    return None
            except Exception as e:
                logging.error(f"Error fetching {url}: {e}")
                return None
        return None

    async def extract_image_details(self, html, image_page_url):
        try:
            soup = BeautifulSoup(html, 'html.parser')

            medium_img_tag = soup.find(id='img')
            medium_quality_image_url = urllib.parse.urljoin(image_page_url, medium_img_tag.get('src')) if medium_img_tag and medium_img_tag.get('src') else None

            medium_quality_div = soup.find(id='i4')
            medium_quality_image_dimensions = None
            medium_quality_image_filesize = None
            if medium_quality_div and medium_quality_div.text.strip():
                match = re.search(r'(\d+ x \d+)\s+::\s+([\d.]+\s+KiB)', medium_quality_div.text.strip())
                if match:
                    medium_quality_image_dimensions = match.group(1)
                    medium_quality_image_filesize = match.group(2)

            return {
                'image_url': medium_quality_image_url,
                'quality': medium_quality_image_dimensions,
                'filesize': medium_quality_image_filesize
            }

        except Exception as e:
            logging.error(f"Error extracting image details from {image_page_url}: {e}")
            return None

    async def get_image_details(self, image_page_url):
        html = await self.fetch_html(image_page_url)
        if html is None:
            return None

        image_details = await self.extract_image_details(html, image_page_url)
        gallery_name = await self.extract_gallery_name(html)

        image_name_match = re.search(r'-(\d+)$', image_page_url)
        image_number = image_name_match.group(1) if image_name_match else "Unknown"

        return {
            "gallery_name": gallery_name,
            "image_number": image_number,
            "image_details": image_details
        }

    async def extract_gallery_name(self, html):
        try:
            soup = BeautifulSoup(html, 'html.parser')
            title_tag = soup.find('h1')
            if title_tag:
                return title_tag.text.strip()
            return "Unknown Gallery Name"
        except Exception as e:
            logging.error(f"Error extracting gallery name: {e}")
            return "Unknown Gallery Name"

    async def extract_bundle_images(self, url):
        html = await self.fetch_html(url)
        if not html:
            return None

        soup = BeautifulSoup(html, 'html.parser')
        bundle_images = {}
        gallery_div = soup.find(id='gdt')

        if gallery_div:
            page_number = 1
            image_count = 0

            for index, element in enumerate(gallery_div.find_all('a', href=True)):
                div = element.find('div')
                style = div.get('style') if div else None
                bundle_image_url = re.search(r'url\((.*?)\)', style).group(1) if style and re.search(r'url\((.*?)\)', style) else None

                gpc_tag = soup.find(class_='gpc')
                total_images_on_page = "Not found"
                if gpc_tag:
                    match = re.search(r'Showing\s*(\d+)\s*-\s*(\d+)\s*of\s*([\d,]+)\s*images', gpc_tag.text.strip())
                    if match:
                        total_images_on_page = int(match.group(2)) - int(match.group(1)) + 1

                if f'page_no_{page_number}' not in bundle_images:
                    bundle_images[f'page_no_{page_number}'] = {
                        'bundle_image': bundle_image_url,
                        'total_image': total_images_on_page
                    }

                image_count += 1
                if image_count % 20 == 0:
                    page_number += 1

        return list(bundle_images.values())

    async def extract_gallery_data(self, url, page_number=1):
        try:
            page_number_to_fetch = int(page_number) if isinstance(page_number, (int, str)) and str(page_number).isdigit() else 1
            current_page = page_number_to_fetch - 1
            current_page_url = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(current_page_url.query)
            if page_number_to_fetch > 1:
                query['p'] = current_page
            new_query_string = urllib.parse.urlencode(query, doseq=True)
            page_url = urllib.parse.urlunparse((current_page_url.scheme, current_page_url.netloc, current_page_url.path,
                                                 current_page_url.params, new_query_string, current_page_url.fragment))

            html = await self.fetch_html(page_url)
            if not html:
                return None

            soup = BeautifulSoup(html, 'html.parser')

            gallery_name = soup.find(id='gn').text.strip() if soup.find(id='gn') else None

            first_bundle_image = soup.select_one('#gdt a div')
            bundle_image_url = re.search(r'url\((.*?)\)', first_bundle_image.get('style')).group(1) if first_bundle_image and first_bundle_image.get('style') and re.search(r'url\((.*?)\)', first_bundle_image.get('style')) else None

            gpc_tag = soup.find(class_='gpc')
            total_images = "Not found"
            if gpc_tag and gpc_tag.text.strip():
                match = re.search(r'of\s*([\d,]+)\s*images', gpc_tag.text.strip())
                if match:
                    total_images = int(match.group(1).replace(',', ''))

            total_pages = "Not found"
            pagination_table = soup.find(class_='ptt')
            if pagination_table:
                a_tags = pagination_table.find_all('a', href=True)
                if len(a_tags) > 1:
                    last_a_tag = a_tags[-2]
                    match = re.search(r'\?p=(\d+)', last_a_tag.get('href'))
                    if match:
                        total_pages = int(match.group(1)) + 1

            image_data = []
            gallery_div = soup.find(id='gdt')
            if gallery_div:
                for index, element in enumerate(gallery_div.find_all('a', href=True)):
                    image_page_url = element.get('href')
                    img_tag = element.find('img')
                    image_url = img_tag.get('src') if img_tag else None

                    file_extension_match = re.search(r'\.([a-zA-Z]+)$', image_url) if image_url else None
                    file_extension = f".{file_extension_match.group(1)}" if file_extension_match else ''

                    image_name_match = re.search(r'-(\d+)$', image_page_url)
                    image_number = image_name_match.group(1) if image_name_match else (index + 1)
                    image_name = f"{gallery_name} No.{image_number}"

                    image_details = await self.get_image_details(image_page_url)
                    image_data.append({
                        'image_page_url': image_page_url,
                        'image_name': f"{image_name}{file_extension}",
                        'image_url': image_details['image_details']['image_url'] if image_details and image_details['image_details'] else None,
                        'quality': image_details['image_details']['quality'] if image_details and image_details['image_details'] else None,
                        'filesize': image_details['image_details']['filesize'] if image_details and image_details['image_details'] else None,
                    })

            return {
                'name': gallery_name,
                'bundle_image_url': bundle_image_url,
                'total_images': total_images,
                'total_pages': total_pages,
                'image_data': image_data,
                'current_page': page_number
            }
        except Exception as e:
            print(f"Error fetching gallery data from {url}: {e}")
            return None


def is_valid_url(url_string):
    try:
        urllib.parse.urlparse(url_string)
        return True
    except:
        return False