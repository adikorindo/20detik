#!/usr/bin/python3
# coding=utf-8
#dev by @dafidxcode

import os, sys, json, time, requests, re, subprocess, random, hashlib, openai
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin
from typing import List, Dict, Optional

# Konfigurasi
BASE_URL = "https://20.detik.com/detikupdate"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
CHECK_INTERVAL = 14400  # 4 jam
DATA_FILE = "posted_videos.json"
DOWNLOAD_DIR = "downloaded_videos"
FB_PAGES_FILE = "facebook_pages.json"
MAX_UPLOADS_PER_CYCLE = 3  # Batas unggahan per siklus
UPLOAD_DELAY_MIN = 20  # Detik
UPLOAD_DELAY_MAX = 40  # Detik
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Daftar CTA acak untuk variasi
CTAS = [
    "Komen pendapatmu di bawah, ya! ",
    "Tag temen yang harus tahu! ",
    "Share ke grupmu, bro! ",
    "Apa sih menurutmu? Tulis di kolom komen! "
]

class FacebookPageManager:
    @staticmethod
    def load_pages() -> List[Dict]:
        """Load Facebook pages configuration from JSON file"""
        if not os.path.exists(FB_PAGES_FILE):
            raise FileNotFoundError(f"Facebook pages config file not found: {FB_PAGES_FILE}")
        
        try:
            with open(FB_PAGES_FILE, 'r') as f:
                pages = json.load(f)
                if not isinstance(pages, list):
                    raise ValueError("Invalid Facebook pages config format - expected list")
                
                required_fields = ['page_id', 'access_token', 'page_name']
                for page in pages:
                    for field in required_fields:
                        if field not in page:
                            raise ValueError(f"Missing required field '{field}' in page config")
                
                return pages
        except Exception as e:
            raise Exception(f"Error loading Facebook pages config: {e}")

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

    def is_video_posted(self, video_url: str, video_hash: str) -> bool:
        """Check if video has already been posted"""
        return any(video.get('source_url') == video_url or video.get('hash') == video_hash for video in self.posted_videos)
    
    def add_posted_video(self, video_details: Dict):
        """Add new video to posted videos list"""
        if not self.is_video_posted(video_details['source_url'], video_details.get('hash', '')):
            self.posted_videos.append(video_details)
            self.save_posted_videos()

    def clean_downloads(self):
        """Remove all downloaded video files"""
        for filename in os.listdir(DOWNLOAD_DIR):
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                print(f"Error deleting file {filename}: {e}")

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
        except Exception as e:
            print(f"Error in video conversion: {e}")
            return None

    @staticmethod
    def get_file_hash(file_path: str) -> str:
        """Calculate MD5 hash of a file"""
        hasher = hashlib.md5()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

class FacebookUploader:
    def __init__(self, page_config: Dict):
        self.page_id = page_config["page_id"]
        self.access_token = page_config["access_token"]
        self.page_name = page_config["page_name"]
        self.api_version = "v20.0"
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})
        self.api_calls = 0
        self.max_api_calls = 180  # Batas aman per jam
        self.last_reset = time.time()

    def make_api_call(self, url, method="get", **kwargs):
        """Make API call with rate limiting"""
        if time.time() - self.last_reset > 3600:
            self.api_calls = 0
            self.last_reset = time.time()
        
        if self.api_calls >= self.max_api_calls:
            print(f"API limit reached for {self.page_name}")
            return None
        
        self.api_calls += 1
        try:
            if method == "get":
                response = self.session.get(url, **kwargs)
            else:
                response = self.session.post(url, **kwargs)
            return response
        except Exception as e:
            print(f"API call error: {e}")
            return None

    def validate_token(self) -> bool:
        """Validate the Facebook access token"""
        url = f"https://graph.facebook.com/{self.api_version}/{self.page_id}/video_reels"
        params = {'since': 'today', 'access_token': self.access_token}
        response = self.make_api_call(url, params=params)
        return response and response.status_code == 200

    def upload_to_all_pages(self, video_path: str, description: str, is_reel: bool = False) -> List[Dict]:
        """Upload video to all configured pages"""
        pages = FacebookPageManager.load_pages()
        results = []
        
        for page in pages:
            try:
                print(f"\nPreparing upload to {page['page_name']}...")
                
                uploader = FacebookUploader(page)
                
                if not uploader.validate_token():
                    print(f"Invalid token for {page['page_name']}")
                    results.append({
                        "page_name": page["page_name"],
                        "status": "failed",
                        "error": "Invalid access token"
                    })
                    continue
                
                post_id = uploader._upload_reel(video_path, description) if is_reel else uploader._upload_regular_video(video_path, description)
                
                if post_id:
                    results.append({
                        "page_name": page["page_name"],
                        "post_id": post_id,
                        "status": "success",
                        "url": f"https://facebook.com/{post_id}"
                    })
                    print(f"Successfully uploaded to {page['page_name']}")
                else:
                    results.append({
                        "page_name": page["page_name"],
                        "status": "failed",
                        "error": "Upload returned no post ID"
                    })
                
                if page != pages[-1]:
                    delay = random.uniform(UPLOAD_DELAY_MIN, UPLOAD_DELAY_MAX)
                    print(f"Waiting {delay:.1f} seconds before next upload...")
                    time.sleep(delay)
                    
            except Exception as e:
                print(f"Error uploading to {page['page_name']}: {e}")
                results.append({
                    "page_name": page["page_name"],
                    "status": "error",
                    "error": str(e)
                })
                continue
                
        return results

    def _upload_reel(self, video_path: str, description: str) -> Optional[str]:
        """Upload a Reel to Facebook"""
        init_url = f"https://graph.facebook.com/{self.api_version}/{self.page_id}/video_reels"
        init_data = {
            'upload_phase': 'start',
            'access_token': self.access_token
        }
        
        try:
            init_response = self.make_api_call(init_url, method="post", data=init_data)
            if not init_response:
                return None
            init_response.raise_for_status()
            video_id = init_response.json().get('video_id')
            
            if not video_id:
                raise Exception("No video ID received from Facebook")

            upload_url = f'https://rupload.facebook.com/video-upload/{self.api_version}/{video_id}'
            headers = {
                'Authorization': f'OAuth {self.access_token}',
                'offset': '0',
                'file_size': str(os.path.getsize(video_path)),
                'Content-Type': 'application/octet-stream'
            }
            
            with open(video_path, 'rb') as video_file:
                upload_response = self.make_api_call(upload_url, method="post", data=video_file, headers=headers)
                if not upload_response:
                    return None
                upload_response.raise_for_status()

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
            
            publish_response = self.make_api_call(init_url, method="post", data=publish_data)
            if not publish_response:
                return None
            publish_response.raise_for_status()
            
            print(f"Reel successfully published to {self.page_name}")
            return video_id
            
        except requests.exceptions.HTTPError as e:
            print(f"HTTP Error uploading Reel: {e.response.text}")
            return None
        except Exception as e:
            print(f"Error uploading Reel: {e}")
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
                
                response = self.make_api_call(url, method="post", files=files, params=params)
                if not response:
                    return None
                response.raise_for_status()
                
                video_id = response.json().get('id')
                print(f"Regular video successfully uploaded to {self.page_name}")
                return video_id
                
        except requests.exceptions.HTTPError as e:
            print(f"HTTP Error uploading video: {e.response.text}")
            return None
        except Exception as e:
            print(f"Error uploading video: {e}")
            return None

class DetikScraper:
    def __init__(self):
        self.headers = {"User-Agent": USER_AGENT}
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})

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
                            full_url = urljoin(BASE_URL, href)
                            video_links.append(full_url)
            
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
            
            title_element = soup.find("h1", class_="detail__title") or soup.find("title")
            title = title_element.get_text(strip=True) if title_element else "No Title"
            
            description = ""
            if desc_div := soup.find("div", class_="detail__body-text"):
                description = desc_div.get_text(strip=True)
            
            hashtags = ""
            if meta_keywords := soup.find("meta", {"name": "keywords"}):
                keywords = [k.strip() for k in meta_keywords["content"].split(",")]
                hashtags = " ".join(f"#{k.replace(' ', '')}" for k in keywords if k)
            
            summarized_description = self.summarize_news(description, hashtags)
            
            duration = 0
            if dur_elem := soup.find('div', class_='media__icon--top-right'):
                dur_text = dur_elem.get_text(strip=True)
                if 'detik' in dur_text:
                    duration = int(dur_text.replace(' detik', ''))
                elif ':' in dur_text:
                    parts = dur_text.split(':')
                    duration = int(parts[0])*60 + int(parts[1])
            
            if not (video_url := self._extract_video_url(response.text)):
                return None
            
            return {
                "title": title,
                "description": summarized_description,
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
            if script_ld := re.search(r'<script type="application/ld\+json">(.*?)</script>', html_content, re.DOTALL):
                try:
                    json_data = json.loads(script_ld.group(1))
                    if json_data.get("@type") == "VideoObject":
                        return json_data.get("contentUrl")
                except json.JSONDecodeError:
                    pass
            
            patterns = [
                r'videoUrl\s*:\s*["\'](.*?\.m3u8[^"\']*)["\']',
                r'<meta[^>]*content=["\'](https?://[^"\']*\.mp4[^"\']*)["\']',
                r'src:\s*["\'](https?://[^"\']*\.mp4[^"\']*)["\']'
            ]
            
            for pattern in patterns:
                if match := re.search(pattern, html_content, re.IGNORECASE):
                    url = match.group(1)
                    if url.startswith('//'):
                        url = 'https:' + url
                    return url
            
            return None
        except Exception as e:
            print(f"URL extraction error: {e}")
            return None

    @staticmethod
    def summarize_news(description: str, keywords: str, max_length: int = 150) -> str:
        """Summarize news with a casual, SEO-friendly style using xAI API"""
        if not OPENAI_API_KEY:
            print("Warning: OPENAI_API_KEY not found, using fallback summary")
            summary = description[:max_length].strip() + "..."
            return f"{summary}\n\n{random.choice(CTAS)}"

        try:
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            prompt = (
                f"Rangkum teks berikut jadi maksimal {max_length} kata dengan gaya santai, kayak cerita ke temen. "
                f"Pastikan terasa alami, manusiawi dan masukkan kata kunci '{keywords}' secara natural untuk SEO. "
                f"Gunakan 1-2 emotikon relevan dan tambahkan CTA acak dari: {', '.join(CTAS)}. "
                f"Buat teks pendek, maksimal 2-3 kalimat dan maksimal 1 paragraf. "
                f"Teks: {description}"
            )

            response = client.chat.completions.create(
                model="gpt-4o-mini",  # Alternatif: "gpt-3.5-turbo"
                messages=[
                    {"role": "system", "content": "Kamu adalah asisten yang membuat ringkasan berita dengan gaya santai dan SEO-friendly."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=200,
                temperature=0.7
            )

            summary = response.choices[0].message.content.strip()
            return summary
        except Exception as e:
            print(f"Error summarizing with OpenAI API: {e}")
            summary = description[:max_length].strip() + "..."
            return f"{summary}\n\n{random.choice(CTAS)}"

def main():
    """Main function to run the scraper and uploader"""
    try:
        print("\n" + "="*50)
        print(f"Detik.com to Facebook Multi-Page Uploader")
        print(f"Started at: {datetime.now()}")
        print("="*50 + "\n")
        
        video_manager = VideoManager()
        scraper = DetikScraper()
        
        try:
            fb_pages = FacebookPageManager.load_pages()
            if not fb_pages:
                raise Exception("No Facebook pages configured in facebook_pages.json")
            
            print(f"Loaded {len(fb_pages)} Facebook pages:")
            for page in fb_pages:
                print(f"- {page['page_name']} (ID: {page['page_id']})")
        except Exception as e:
            print(f"Error loading Facebook pages: {e}")
            return

        while True:
            try:
                print("\n" + "="*50)
                print(f"[{datetime.now()}] Checking for new videos...")
                
                video_links = scraper.get_video_links()
                if not video_links:
                    print("No videos found, waiting for next check...")
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                print(f"Found {len(video_links)} video links")
                
                new_videos = 0
                for link in video_links:
                    if new_videos >= MAX_UPLOADS_PER_CYCLE:
                        print(f"Reached max uploads ({MAX_UPLOADS_PER_CYCLE}) for this cycle.")
                        break
                    
                    try:
                        details = scraper.get_video_details(link)
                        if not details:
                            print("Failed to get video details, skipping...")
                            continue
                        
                        video_hash = ""
                        if video_manager.is_video_posted(details['source_url'], video_hash):
                            print(f"\nSkipping already posted video: {link}")
                            continue
                        
                        print(f"\nProcessing new video: {link}")
                        print(f"Title: {details['title']}")
                        print(f"Duration: {details['duration']} seconds")
                        
                        print("Downloading video...")
                        original_path = VideoProcessor.download_video(details['source_url'])
                        if not original_path:
                            print("Failed to download video, skipping...")
                            continue
                        
                        video_hash = VideoProcessor.get_file_hash(original_path)
                        if video_manager.is_video_posted(details['source_url'], video_hash):
                            print(f"\nSkipping duplicate video (hash: {video_hash})")
                            os.remove(original_path)
                            continue
                        
                        is_reel = details['duration'] <= 60
                        if is_reel:
                            print("Video is short (<= 60s), converting to Reel format...")
                            video_path = VideoProcessor.convert_to_reel_format(original_path)
                            os.remove(original_path)
                            if not video_path:
                                continue
                        else:
                            video_path = original_path
                            print("Video is long (> 60s), uploading as regular video...")
                        
                        description = details['description']
                        
                        print("\nStarting upload to all pages...")
                        uploader = FacebookUploader(fb_pages[0])
                        upload_results = uploader.upload_to_all_pages(video_path, description, is_reel)
                        
                        success_count = sum(1 for r in upload_results if r['status'] == 'success')
                        if success_count > 0:
                            details["posted_to"] = upload_results
                            details["hash"] = video_hash
                            video_manager.add_posted_video(details)
                            new_videos += 1
                            print(f"\nSuccessfully uploaded to {success_count} page(s)")
                        else:
                            print("\nFailed to upload to all pages")
                        
                        try:
                            os.remove(video_path)
                            print("Cleaned up video file")
                        except Exception as e:
                            print(f"Error cleaning up video file: {e}")
                        
                        print("\nUpload results:")
                        for result in upload_results:
                            status = "✓" if result['status'] == 'success' else "✗"
                            print(f"{status} {result['page_name']}: {result.get('url', 'Failed')}")
                            if 'error' in result:
                                print(f"   Error: {result['error']}")
                    
                    except Exception as e:
                        print(f"\nError processing video: {e}")
                        continue
                
                print(f"\nCycle completed. {new_videos} new videos processed.")
                print(f"Next check in {CHECK_INTERVAL//3600} hours...")
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
