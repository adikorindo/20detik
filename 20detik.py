import os
import sys
import json
import time
import requests
import re
import subprocess
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin
from typing import List, Dict

# Konfigurasi
BASE_URL = "https://20.detik.com/detikupdate"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
CHECK_INTERVAL = 3600  # 1 jam
DATA_FILE = "posted_videos.json"
DOWNLOAD_DIR = "downloaded_videos"
FB_PAGES_FILE = "facebook_pages.json"
MAX_RETRIES = 3

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

class FacebookPageManager:
    @staticmethod
    def load_pages() -> List[Dict]:
        if not os.path.exists(FB_PAGES_FILE):
            raise FileNotFoundError(f"Facebook pages config file not found: {FB_PAGES_FILE}")
            
        with open(FB_PAGES_FILE, 'r') as f:
            pages = json.load(f)
            if not isinstance(pages, list):
                raise ValueError("Invalid Facebook pages config format - expected list")
            return pages

class VideoManager:
    def __init__(self):
        self.data_file = DATA_FILE
        self.posted_videos = self.load_posted_videos()

    def load_posted_videos(self) -> List[Dict]:
        if not os.path.exists(self.data_file):
            return []
            
        try:
            with open(self.data_file, 'r') as f:
                if os.stat(self.data_file).st_size == 0:
                    return []                
                return json.load(f)
        except Exception:
            return []

    def save_posted_videos(self):
        with open(self.data_file, 'w') as f:
            json.dump(self.posted_videos, f, indent=2)

    def is_video_posted(self, video_url: str) -> bool:
        return any(video.get('source_url') == video_url for video in self.posted_videos)
        
    def add_posted_video(self, video_details: Dict):
        if not self.is_video_posted(video_details['source_url']):
            self.posted_videos.append(video_details)
            self.save_posted_videos()

    def clean_downloads(self):
        for filename in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception:
                pass

class VideoProcessor:
    @staticmethod
    def download_video(video_url: str) -> str:
        try:
            import yt_dlp
            
            ydl_opts = {
                'format': 'bestvideo[height<=1080]+bestaudio/best',
                'outtmpl': os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s'),
                'merge_output_format': 'mp4',
                'quiet': True,
                'no_warnings': True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
                return ydl.prepare_filename(info)

        except Exception as e:
            raise Exception(f"Download error: {e}")

    @staticmethod
    def convert_to_reel_format(input_path: str) -> str:
        output_path = os.path.join(DOWNLOAD_DIR, "reel_" + os.path.basename(input_path))
        
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-vf', 'scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1',
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            '-f', 'mp4',
            output_path
        ]
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return output_path
        except subprocess.CalledProcessError as e:
            raise Exception(f"FFmpeg conversion failed: {e.stderr.decode()}")

class ReelUploader:
    def __init__(self, page_config: Dict):
        self.page_id = page_config["page_id"]
        self.access_token = page_config["access_token"]
        self.page_name = page_config["page_name"]
        self.api_version = "v20.0"
        self.session = requests.Session()

    def validate_token(self) -> bool:
        url = f"https://graph.facebook.com/{self.api_version}/me/accounts"
        params = {'access_token': self.access_token}
        response = self.session.get(url, params=params)
        return response.status_code == 200

    def upload_reel(self, video_path: str, description: str) -> str:
        if not self.validate_token():
            raise Exception("Invalid or expired access token")

        # Step 1: Initialize upload
        init_url = f"https://graph.facebook.com/{self.api_version}/{self.page_id}/video_reels"
        init_data = {
            'upload_phase': 'start',
            'access_token': self.access_token
        }
        init_response = self.session.post(init_url, data=init_data)
        
        if init_response.status_code != 200:
            raise Exception(f"Initialize failed: {init_response.text}")
        
        video_id = init_response.json().get('video_id')
        if not video_id:
            raise Exception("No video ID received")

        # Step 2: Upload video data
        upload_url = f'https://rupload.facebook.com/video-upload/{self.api_version}/{video_id}'
        headers = {
            'Authorization': f'OAuth {self.access_token}',
            'offset': '0',
            'file_size': str(os.path.getsize(video_path)),
            'Content-Type': 'application/octet-stream'
        }
        
        with open(video_path, 'rb') as video_file:
            upload_response = self.session.post(upload_url, data=video_file, headers=headers)
            if upload_response.status_code != 200:
                raise Exception(f"Upload failed: {upload_response.text}")

        # Step 3: Publish with Reels parameters
        publish_data = {
            'access_token': self.access_token,
            'video_id': video_id,
            'upload_phase': 'finish',
            'description': description,
            'video_state': 'PUBLISHED',
            'container_type': 'REELS',
            'share_to_feed': 'true',
            'allow_share_to_stories': 'true',
            'crossposting_original_video_id': video_id
        }
        
        publish_response = self.session.post(init_url, data=publish_data)
        if publish_response.status_code != 200:
            raise Exception(f"Publish failed: {publish_response.text}")
        
        # Step 4: Verify processing status
        if self.check_processing_status(video_id):
            return video_id
        else:
            raise Exception("Reel failed to process within timeout period")

    def check_processing_status(self, video_id: str, timeout: int = 300) -> bool:
        check_url = f"https://graph.facebook.com/{self.api_version}/{video_id}"
        params = {
            'fields': 'status,permalink_url',
            'access_token': self.access_token
        }
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            response = self.session.get(check_url, params=params)
            if response.status_code == 200:
                data = response.json()
                status = data.get('status', {}).get('video_status')
                
                if status == 'ready':
                    print(f"Reel is live: {data.get('permalink_url')}")
                    return True
                elif status == 'processing':
                    print("Reel is still processing...")
                else:
                    print(f"Unexpected status: {status}")
            
            time.sleep(30)
        
        return False

class DetikScraper:
    def __init__(self):
        self.headers = {"User-Agent": USER_AGENT}
        self.session = requests.Session()

    def get_video_links(self) -> List[str]:
        try:
            response = self.session.get(BASE_URL, headers=self.headers)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            video_links = []
            
            for article in soup.find_all("article", class_="list-content__item"):
                if link := article.find("a", class_="block-link"):
                    if href := link.get("href"):
                        if "video" in href.lower():
                            video_links.append(urljoin(BASE_URL, href))
            
            return list(set(video_links))
        except Exception as e:
            raise Exception(f"Scraping error: {e}")

    def get_video_details(self, video_url: str) -> Optional[Dict]:
        try:
            response = self.session.get(video_url, headers=self.headers)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            title = (soup.find("h1", class_="detail__title") or soup.find("title")).get_text(strip=True)
            
            description = ""
            if desc_div := soup.find("div", class_="detail__body-text"):
                description = desc_div.get_text(strip=True)
            
            duration = 0
            if dur_elem := soup.find('div', class_='media__icon--top-right'):
                dur_text = dur_elem.get_text(strip=True)
                if 'detik' in dur_text:
                    duration = int(dur_text.replace(' detik', ''))
                elif ':' in dur_text:
                    parts = dur_text.split(':')
                    duration = int(parts[0])*60 + int(parts[1])
            
            hashtags = ""
            if meta_keywords := soup.find("meta", {"name": "keywords"}):
                keywords = [k.strip() for k in meta_keywords["content"].split(",")]
                hashtags = " ".join(f"#{k.replace(' ', '')}" for k in keywords if k)
            
            if not (video_url := self._extract_video_url(response.text)):
                return None
            
            return {
                "title": title,
                "description": description,
                "duration": duration,
                "keywords": hashtags,
                "source_url": video_url,
                "scraped_at": datetime.now().isoformat()
            }
        except Exception as e:
            raise Exception(f"Error processing {video_url}: {e}")

    def _extract_video_url(self, html_content: str) -> Optional[str]:
        try:
            if script_ld := re.search(r'<script type="application/ld\+json">(.*?)</script>', html_content, re.DOTALL):
                if json_data := json.loads(script_ld.group(1)):
                    if json_data.get("@type") == "VideoObject":
                        return json_data.get("contentUrl")
            
            patterns = [
                r'videoUrl\s*:\s*["\'](.*?\.m3u8[^"\']*)["\']',
                r'<meta[^>]*content=["\'](https?://[^"\']*\.mp4[^"\']*)["\']'
            ]
            
            for pattern in patterns:
                if match := re.search(pattern, html_content, re.IGNORECASE):
                    return match.group(1)
            
            return None
        except Exception as e:
            raise Exception(f"URL extraction error: {e}")

def main():
    try:
        print("Starting Detik.com Scraper...")
        print(f"Current time: {datetime.now()}")
        
        fb_pages = FacebookPageManager.load_pages()
        if not fb_pages:
            raise Exception("No Facebook pages configured")
        
        video_manager = VideoManager()
        scraper = DetikScraper()
        
        while True:
            try:
                print("\n" + "="*50)
                print(f"[{datetime.now()}] Checking for new videos...")
                
                video_links = scraper.get_video_links()
                if not video_links:
                    print("No videos found, waiting for next check...")
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                new_videos = 0
                for link in video_links:
                    if video_manager.is_video_posted(link):
                        print(f"Skipping already posted video: {link}")
                        continue
                    
                    print(f"\nProcessing new video: {link}")
                    
                    for attempt in range(MAX_RETRIES):
                        try:
                            details = scraper.get_video_details(link)
                            if not details:
                                print("Failed to extract video details")
                                break
                            
                            print(f"Downloading: {details['title']}")
                            original_path = VideoProcessor.download_video(details['source_url'])
                            
                            is_reel = details['duration'] <= 60
                            if is_reel:
                                print("Converting to Reel format...")
                                video_path = VideoProcessor.convert_to_reel_format(original_path)
                                os.remove(original_path)
                            else:
                                video_path = original_path
                            
                            description = (
                                f"{details['title']}\n\n"
                                f"{details['description']}\n\n"
                                f"{details['keywords']}\n\n"
                                f"Sumber: {link}"
                            )
                            
                            upload_results = []
                            for page in fb_pages:
                                try:
                                    print(f"\nUploading to {page['page_name']}...")
                                    uploader = ReelUploader(page) if is_reel else FacebookUploader(page)
                                    
                                    if is_reel:
                                        post_id = uploader.upload_reel(video_path, description)
                                    else:
                                        post_id = uploader.upload_video(video_path, description)
                                    
                                    if post_id:
                                        upload_results.append({
                                            "page_id": page["page_id"],
                                            "page_name": page["page_name"],
                                            "post_id": post_id,
                                            "is_reel": is_reel,
                                            "timestamp": datetime.now().isoformat()
                                        })
                                        print(f"Successfully uploaded to {page['page_name']}")
                                        time.sleep(30)  # Delay between page uploads
                                    
                                except Exception as e:
                                    print(f"Failed to upload to {page['page_name']}: {str(e)}")
                                    continue
                            
                            if upload_results:
                                details["posted_to"] = upload_results
                                video_manager.add_posted_video(details)
                                new_videos += 1
                                print(f"Successfully processed: {details['title']}")
                            
                            os.remove(video_path)
                            break
                            
                        except Exception as e:
                            print(f"Attempt {attempt + 1} failed: {str(e)}")
                            if attempt == MAX_RETRIES - 1:
                                print("Max retries reached, skipping this video")
                            time.sleep(10)
                            continue
                
                print(f"\nCycle completed. {new_videos} new videos processed.")
                print(f"Next check in {CHECK_INTERVAL//60} minutes...")
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print("\nReceived keyboard interrupt, exiting gracefully...")
                break
            except Exception as e:
                print(f"\nError in main loop: {str(e)}")
                print("Retrying in 5 minutes...")
                time.sleep(300)

    except Exception as e:
        print(f"\nFatal error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()