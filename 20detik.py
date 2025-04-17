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
from typing import List, Dict, Optional

# Configuration
BASE_URL = "https://20.detik.com/detikupdate"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
CHECK_INTERVAL = 3600  # 1 hour
DATA_FILE = "posted_videos.json"
DOWNLOAD_DIR = "downloaded_videos"
FB_PAGES_FILE = "facebook_pages.json"
MAX_RETRIES = 3

# Ensure download directory exists
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

class FacebookPageManager:
    @staticmethod
    def load_pages() -> List[Dict]:
        """Load Facebook pages configuration from JSON file"""
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
        """Load posted videos from JSON file"""
        if not os.path.exists(self.data_file):
            return []
            
        try:
            with open(self.data_file, 'r') as f:
                if os.stat(self.data_file).st_size == 0:
                    return []                
                return json.load(f)
        except Exception as e:
            print(f"Error loading posted videos: {e}")
            return []

    def save_posted_videos(self):
        """Save posted videos to JSON file"""
        try:
            with open(self.data_file, 'w') as f:
                json.dump(self.posted_videos, f, indent=2)
        except Exception as e:
            print(f"Error saving posted videos: {e}")

    def is_video_posted(self, video_url: str) -> bool:
        """Check if video has already been posted"""
        return any(video.get('source_url') == video_url for video in self.posted_videos)
        
    def add_posted_video(self, video_details: Dict):
        """Add new video to posted videos list"""
        if not self.is_video_posted(video_details['source_url']):
            self.posted_videos.append(video_details)
            self.save_posted_videos()

class VideoProcessor:
    @staticmethod
    def download_video(video_url: str) -> Optional[str]:
        """Download video using yt-dlp"""
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
            print(f"Download error: {e}")
            return None

    @staticmethod
    def convert_to_reel_format(input_path: str) -> Optional[str]:
        """Convert video to Reels format using FFmpeg"""
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
            print(f"FFmpeg conversion failed: {e.stderr.decode()}")
            return None

class FacebookUploader:
    def __init__(self, page_config: Dict):
        self.page_id = page_config["page_id"]
        self.access_token = page_config["access_token"]
        self.page_name = page_config["page_name"]
        self.api_version = "v20.0"
        self.session = requests.Session()

    def upload_video(self, video_path: str, description: str, is_reel: bool = False) -> Optional[str]:
        """Upload video to Facebook (regular video or Reel)"""
        try:
            if is_reel:
                return self._upload_reel(video_path, description)
            else:
                return self._upload_regular_video(video_path, description)
        except Exception as e:
            print(f"Upload error to {self.page_name}: {e}")
            return None

    def _upload_reel(self, video_path: str, description: str) -> Optional[str]:
        """Upload a Reel to Facebook"""
        # Step 1: Initialize upload
        init_url = f"https://graph.facebook.com/{self.api_version}/{self.page_id}/video_reels"
        init_data = {
            'upload_phase': 'start',
            'access_token': self.access_token
        }
        
        try:
            init_response = self.session.post(init_url, data=init_data)
            init_response.raise_for_status()
            video_id = init_response.json().get('video_id')
            
            if not video_id:
                raise Exception("No video ID received from Facebook")

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
                upload_response.raise_for_status()

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
            publish_response.raise_for_status()
            
            print(f"Successfully uploaded Reel to {self.page_name}")
            return video_id
            
        except Exception as e:
            print(f"Reel upload failed: {e}")
            return None

    def _upload_regular_video(self, video_path: str, description: str) -> Optional[str]:
        """Upload a regular video to Facebook"""
        url = f"https://graph-video.facebook.com/{self.api_version}/{self.page_id}/videos"
        
        try:
            with open(video_path, 'rb') as video_file:
                files = {'source': video_file}
                params = {
                    'access_token': self.access_token,
                    'description': description,
                    'published': 'true'
                }
                
                response = self.session.post(url, files=files, params=params)
                response.raise_for_status()
                
                video_id = response.json().get('id')
                print(f"Successfully uploaded video to {self.page_name}")
                return video_id
                
        except Exception as e:
            print(f"Regular video upload failed: {e}")
            return None

class DetikScraper:
    def __init__(self):
        self.headers = {"User-Agent": USER_AGENT}
        self.session = requests.Session()

    def get_video_links(self) -> List[str]:
        """Get video links from Detik.com"""
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
            print(f"Scraping error: {e}")
            return []

    def get_video_details(self, video_url: str) -> Optional[Dict]:
        """Get details of a specific video"""
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
            print(f"Error processing {video_url}: {e}")
            return None

    def _extract_video_url(self, html_content: str) -> Optional[str]:
        """Extract video URL from page content"""
        try:
            # Try JSON-LD first
            if script_ld := re.search(r'<script type="application/ld\+json">(.*?)</script>', html_content, re.DOTALL):
                if json_data := json.loads(script_ld.group(1)):
                    if json_data.get("@type") == "VideoObject":
                        return json_data.get("contentUrl")
            
            # Try other patterns
            patterns = [
                r'videoUrl\s*:\s*["\'](.*?\.m3u8[^"\']*)["\']',
                r'<meta[^>]*content=["\'](https?://[^"\']*\.mp4[^"\']*)["\']'
            ]
            
            for pattern in patterns:
                if match := re.search(pattern, html_content, re.IGNORECASE):
                    return match.group(1)
            
            return None
        except Exception as e:
            print(f"URL extraction error: {e}")
            return None

def main():
    """Main function to run the scraper and uploader"""
    try:
        print("Starting Detik.com Scraper...")
        print(f"Current time: {datetime.now()}")
        
        # Load Facebook pages configuration
        try:
            fb_pages = FacebookPageManager.load_pages()
            if not fb_pages:
                raise Exception("No Facebook pages configured in facebook_pages.json")
        except Exception as e:
            print(f"Error loading Facebook pages: {e}")
            return

        video_manager = VideoManager()
        scraper = DetikScraper()
        
        while True:
            try:
                print("\n" + "="*50)
                print(f"[{datetime.now()}] Checking for new videos...")
                
                # Get video links
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
                    
                    # Get video details
                    details = scraper.get_video_details(link)
                    if not details:
                        print("Failed to get video details")
                        continue
                    
                    print(f"Title: {details['title']}")
                    print(f"Duration: {details['duration']} seconds")
                    
                    # Download video
                    print("Downloading video...")
                    original_path = VideoProcessor.download_video(details['source_url'])
                    if not original_path:
                        print("Failed to download video")
                        continue
                    
                    # Process video based on duration
                    is_reel = details['duration'] <= 60
                    if is_reel:
                        print("Converting to Reel format...")
                        video_path = VideoProcessor.convert_to_reel_format(original_path)
                        os.remove(original_path)
                        if not video_path:
                            continue
                    else:
                        video_path = original_path
                    
                    # Prepare description
                    description = (
                        f"{details['title']}\n\n"
                        f"{details['description']}\n\n"
                        f"{details['keywords']}\n\n"
                        f"Sumber: {link}"
                    )
                    
                    # Upload to all configured pages
                    upload_results = []
                    for page in fb_pages:
                        try:
                            print(f"\nUploading to {page['page_name']}...")
                            uploader = FacebookUploader(page)
                            post_id = uploader.upload_video(video_path, description, is_reel)
                            
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
                            else:
                                print(f"Failed to upload to {page['page_name']}")
                        
                        except Exception as e:
                            print(f"Error uploading to {page['page_name']}: {e}")
                            continue
                    
                    # Clean up and record results
                    if upload_results:
                        details["posted_to"] = upload_results
                        video_manager.add_posted_video(details)
                        new_videos += 1
                        print(f"Successfully processed: {details['title']}")
                    
                    try:
                        os.remove(video_path)
                    except Exception as e:
                        print(f"Error cleaning up video file: {e}")
                
                print(f"\nCycle completed. {new_videos} new videos processed.")
                print(f"Next check in {CHECK_INTERVAL//60} minutes...")
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                print("\nReceived keyboard interrupt, exiting gracefully...")
                break
            except Exception as e:
                print(f"\nError in main loop: {e}")
                print("Retrying in 5 minutes...")
                time.sleep(300)

    except Exception as e:
        print(f"\nFatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
