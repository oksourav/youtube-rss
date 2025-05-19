import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import re
from urllib.parse import urlparse, parse_qs
from flask import Flask, Response, jsonify, render_template_string
import concurrent.futures
import logging
import os
import time
from typing import List, Dict, Optional, Tuple
import json
from dotenv import load_dotenv

load_dotenv()  # take environment variables from .env.

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration - Environment variables for Render deployment
CHANNELS = os.getenv('YOUTUBE_CHANNELS', '').split(',') if os.getenv('YOUTUBE_CHANNELS') else []
CHANNEL_USERNAMES = os.getenv('YOUTUBE_USERNAMES', '').split(',') if os.getenv('YOUTUBE_USERNAMES') else []
PORT = int(os.getenv('PORT', 5000))
DEBUG_MODE = os.getenv('DEBUG', 'false').lower() == 'true'

# Filter configuration
FILTER_CONFIG = {
    'strict_mode': os.getenv('STRICT_FILTER', 'false').lower() == 'true',
    'max_short_duration': int(os.getenv('MAX_SHORT_DURATION', '90')),  # 90 seconds by default
    'include_duration_in_title': os.getenv('INCLUDE_DURATION', 'true').lower() == 'true',
}

# Statistics tracking
STATS = {
    'total_requests': 0,
    'total_entries_processed': 0,
    'total_shorts_filtered': 0,
    'last_updated': None,
    'channels_processed': 0,
    'processing_time': 0
}

class YouTubeRSSOptimizer:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; FeedlyBot/1.0; +http://www.feedly.com/fetcher.html)',
            'Accept': 'application/rss+xml, application/xml, text/xml'
        })
        self.yt_api_key = os.getenv('YOUTUBE_API_KEY')  # Optional for duration lookup
    
    def get_channel_rss_url(self, channel_id: Optional[str] = None, username: Optional[str] = None) -> str:
        """Generate RSS URL for a channel"""
        if channel_id:
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        elif username:
            return f"https://www.youtube.com/feeds/videos.xml?user={username}"
        else:
            raise ValueError("Either channel_id or username must be provided")
    
    def fetch_rss_feed(self, url: str) -> Optional[str]:
        """Fetch RSS feed from URL with retry logic"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=15)
                response.raise_for_status()
                return response.text
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to fetch RSS after {max_retries} attempts from {url}: {e}")
                    return None
                time.sleep(2 ** attempt)  # Exponential backoff
        return None
    
    def extract_video_id(self, link: str) -> Optional[str]:
        """Extract video ID from YouTube URL"""
        patterns = [
            r'watch\?v=([a-zA-Z0-9_-]{11})',
            r'youtu\.be/([a-zA-Z0-9_-]{11})',
            r'embed/([a-zA-Z0-9_-]{11})'
        ]
        for pattern in patterns:
            match = re.search(pattern, link)
            if match:
                return match.group(1)
        return None
    
    def get_video_duration_from_api(self, video_id: str) -> Optional[int]:
        """Get video duration using YouTube API (optional)"""
        if not self.yt_api_key:
            return None
        try:
            url = f"https://www.googleapis.com/youtube/v3/videos?id={video_id}&part=contentDetails&key={self.yt_api_key}"
            response = self.session.get(url, timeout=10)
            data = response.json()
            if 'items' in data and len(data['items']) > 0:
                duration_str = data['items'][0]['contentDetails']['duration']
                return self.parse_youtube_duration(duration_str)
        except Exception as e:
            logger.debug(f"API duration lookup failed for {video_id}: {e}")
        return None
    
    def parse_youtube_duration(self, duration_str: str) -> Optional[int]:
        """Parse YouTube API duration format (PT4M13S) to seconds"""
        if not duration_str.startswith('PT'):
            return None
        
        duration_str = duration_str[2:]  # Remove 'PT'
        hours = minutes = seconds = 0
        
        # Parse hours
        if 'H' in duration_str:
            hours_match = re.search(r'(\d+)H', duration_str)
            if hours_match:
                hours = int(hours_match.group(1))
        
        # Parse minutes
        if 'M' in duration_str:
            minutes_match = re.search(r'(\d+)M', duration_str)
            if minutes_match:
                minutes = int(minutes_match.group(1))
        
        # Parse seconds
        if 'S' in duration_str:
            seconds_match = re.search(r'(\d+)S', duration_str)
            if seconds_match:
                seconds = int(seconds_match.group(1))
        
        return hours * 3600 + minutes * 60 + seconds
    
    def parse_duration_from_title(self, title: str) -> Optional[int]:
        """Extract duration from video title if present"""
        patterns = [
            r'[\[\(](\d{1,2}):(\d{2})[\]\)]',  # [2:30] or (5:45)
            r'[\[\(](\d{1,2}):(\d{2}):(\d{2})[\]\)]',  # [1:23:45]
            r'(\d{1,2})min(?:\s*(\d{1,2})s)?',  # 5min 30s
            r'(\d{1,2})h(?:\s*(\d{1,2})min)?'  # 1h 30min
        ]
        
        for pattern in patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                groups = match.groups()
                if len(groups) == 2:  # MM:SS format
                    minutes, seconds = map(int, groups)
                    return minutes * 60 + seconds
                elif len(groups) == 3:  # HH:MM:SS format
                    hours, minutes, seconds = map(int, groups)
                    return hours * 3600 + minutes * 60 + seconds
        return None
    
    def format_duration(self, seconds: int) -> str:
        """Format seconds into human readable duration"""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            remaining_seconds = seconds % 60
            if remaining_seconds == 0:
                return f"{minutes}m"
            return f"{minutes}m{remaining_seconds}s"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            if minutes == 0:
                return f"{hours}h"
            return f"{hours}h{minutes}m"
    
    def is_likely_short(self, entry, title: str, description: str, video_link: str) -> Tuple[bool, str]:
        """Determine if a video is likely a Short with reasoning"""
        reasons = []
        
        # Handle None values safely
        title_clean = (title or '').strip()
        desc_clean = (description or '').strip()
        title_lower = title_clean.lower()
        desc_lower = desc_clean.lower()
        
        # Check for explicit Short indicators
        short_indicators = [
            '#shorts', '#short', '#youtubeshorts', '#ytshorts',
            ' shorts', ' short', 'youtube shorts'
        ]
        
        for indicator in short_indicators:
            if indicator in title_lower or indicator in desc_lower:
                reasons.append(f"Contains '{indicator}' keyword")
                if not FILTER_CONFIG['strict_mode']:
                    return True, "; ".join(reasons)
        
        # Check duration from title
        title_duration = self.parse_duration_from_title(title_clean) if title_clean else None
        if title_duration and title_duration <= FILTER_CONFIG['max_short_duration']:
            reasons.append(f"Duration from title: {self.format_duration(title_duration)}")
            return True, "; ".join(reasons)
        
        # Optional: Use YouTube API for exact duration
        if self.yt_api_key:
            video_id = self.extract_video_id(video_link)
            if video_id:
                api_duration = self.get_video_duration_from_api(video_id)
                if api_duration and api_duration <= FILTER_CONFIG['max_short_duration']:
                    reasons.append(f"API duration: {self.format_duration(api_duration)}")
                    return True, "; ".join(reasons)
        
        # In strict mode, check additional heuristics
        if FILTER_CONFIG['strict_mode']:
            # Check for very short titles (often associated with Shorts)
            if title_clean and len(title_clean) < 15:
                reasons.append("Very short title")
            
            # Check for typical Short description patterns
            short_desc_patterns = [
                r'#shorts', r'short video', r'quick.*tip', r'in.*seconds?'
            ]
            for pattern in short_desc_patterns:
                if re.search(pattern, desc_lower):
                    reasons.append(f"Description pattern: {pattern}")
                    return True, "; ".join(reasons)
        
        return len(reasons) > 0, "; ".join(reasons) if reasons else "Not detected as Short"
    
    def enhance_entry_for_feedly(self, entry, video_data: Dict) -> None:
        """Enhance RSS entry with Feedly-optimized metadata"""
        namespace = '{http://www.w3.org/2005/Atom}'
        media_namespace = '{http://search.yahoo.com/mrss/}'
        
        # Add duration to title if configured and available
        if FILTER_CONFIG['include_duration_in_title']:
            title_elem = entry.find('title')
            if title_elem is not None:
                original_title = title_elem.text or ''
                
                # Try to get duration
                duration = None
                duration_from_title = self.parse_duration_from_title(original_title)
                
                if duration_from_title:
                    duration = duration_from_title
                elif self.yt_api_key and video_data.get('video_id'):
                    api_duration = self.get_video_duration_from_api(video_data['video_id'])
                    if api_duration:
                        duration = api_duration
                
                if duration:
                    duration_str = self.format_duration(duration)
                    # Only add duration if not already present
                    if not re.search(r'[\[\(]\d+[:\d]*[ms]?[\]\)]', original_title):
                        title_elem.text = f"[{duration_str}] {original_title}"
        
        # Add custom elements for RSS readers
        # Add category for better organization
        category_elem = ET.SubElement(entry, f'{namespace}category')
        category_elem.set('term', 'video')
        category_elem.set('label', 'Video Content')
        
        # Add media:rating for content ratings
        if entry.find(f'{media_namespace}group') is not None:
            group_elem = entry.find(f'{media_namespace}group')
            if group_elem.find(f'{media_namespace}rating') is None:
                rating_elem = ET.SubElement(group_elem, f'{media_namespace}rating')
                rating_elem.text = 'nonadult'
                rating_elem.set('scheme', 'urn:simple')
        
        # Enhanced summary with metadata
        summary_elem = entry.find(f'{namespace}summary')
        if summary_elem is not None:
            original_summary = summary_elem.text or ''
            enhanced_summary = original_summary
            
            # Add metadata footer
            if video_data.get('author'):
                enhanced_summary += f"\n\n📺 Channel: {video_data['author']}"
            if video_data.get('published'):
                try:
                    pub_date = datetime.fromisoformat(video_data['published'].replace('Z', '+00:00'))
                    enhanced_summary += f"\n📅 Published: {pub_date.strftime('%Y-%m-%d')}"
                except:
                    pass
            
            summary_elem.text = enhanced_summary
    
    def parse_rss_entry(self, entry, namespace: str) -> Dict:
        """Parse individual RSS entry with enhanced data extraction"""
        video_data = {}
        
        # Extract basic info with safe handling
        title_elem = entry.find('title')
        video_data['title'] = title_elem.text.strip() if title_elem is not None and title_elem.text else ''
        
        link_elem = entry.find('link')
        video_data['link'] = link_elem.get('href') if link_elem is not None else ''
        video_data['video_id'] = self.extract_video_id(video_data['link'])
        
        published_elem = entry.find('published')
        video_data['published'] = published_elem.text if published_elem is not None and published_elem.text else ''
        
        # Extract description with safe handling
        description_elem = entry.find(f'{namespace}group/{namespace}description')
        video_data['description'] = description_elem.text.strip() if description_elem is not None and description_elem.text else ''
        
        # Extract thumbnail
        thumbnail_elem = entry.find(f'{namespace}group/{namespace}thumbnail')
        video_data['thumbnail'] = thumbnail_elem.get('url') if thumbnail_elem is not None else ''
        
        # Extract author with safe handling
        author_elem = entry.find('author/name')
        video_data['author'] = author_elem.text.strip() if author_elem is not None and author_elem.text else ''
        
        return video_data
    
    def filter_shorts_from_feed(self, rss_content: str) -> Optional[str]:
        """Filter Shorts from RSS feed content with statistics tracking"""
        start_time = time.time()
        try:
            root = ET.fromstring(rss_content)
            
            # Define namespaces
            namespace = '{http://www.w3.org/2005/Atom}'
            media_namespace = '{http://search.yahoo.com/mrss/}'
            
            # Find all entries
            entries = root.findall(f'{namespace}entry')
            filtered_entries = []
            shorts_filtered = 0
            
            logger.info(f"Processing {len(entries)} entries")
            STATS['total_entries_processed'] += len(entries)
            
            for entry in entries:
                try:
                    video_data = self.parse_rss_entry(entry, media_namespace)
                    
                    # Check if this is likely a Short
                    is_short, reason = self.is_likely_short(
                        entry, 
                        video_data['title'], 
                        video_data['description'],
                        video_data['link']
                    )
                    
                    if not is_short:
                        # Enhance entry for Feedly
                        self.enhance_entry_for_feedly(entry, video_data)
                        filtered_entries.append(entry)
                    else:
                        shorts_filtered += 1
                        logger.info(f"Filtered Short: {video_data.get('title', 'No title')} - Reason: {reason}")
                        
                except Exception as e:
                    logger.error(f"Error processing entry: {e}")
                    # Include the entry anyway if we can't process it
                    filtered_entries.append(entry)
            
            STATS['total_shorts_filtered'] += shorts_filtered
            logger.info(f"Kept {len(filtered_entries)} out of {len(entries)} entries (filtered {shorts_filtered} shorts)")
            
            # Remove original entries and add filtered ones
            for entry in entries:
                root.remove(entry)
            
            for entry in filtered_entries:
                root.append(entry)
            
            # Update feed metadata
            self.update_feed_metadata(root, len(filtered_entries), shorts_filtered)
            
            STATS['processing_time'] += time.time() - start_time
            return ET.tostring(root, encoding='unicode')
        
        except ET.ParseError as e:
            logger.error(f"Error parsing RSS: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error filtering RSS: {e}")
            return None
    
    def update_feed_metadata(self, root, kept_entries: int, filtered_shorts: int):
        """Update feed metadata with processing information"""
        namespace = '{http://www.w3.org/2005/Atom}'
        
        # Update title
        title_elem = root.find(f'{namespace}title')
        if title_elem is not None:
            title_elem.text = f'Filtered YouTube Feed (No Shorts) - {kept_entries} videos'
        
        # Update or add subtitle with statistics
        subtitle_elem = root.find(f'{namespace}subtitle')
        if subtitle_elem is None:
            subtitle_elem = ET.SubElement(root, f'{namespace}subtitle')
        subtitle_elem.text = f'Filtered {filtered_shorts} shorts from recent videos. Optimized for Feedly.'
        
        # Update timestamp
        updated_elem = root.find(f'{namespace}updated')
        if updated_elem is not None:
            updated_elem.text = datetime.now(timezone.utc).isoformat()
        
        # Add generator information
        generator_elem = root.find(f'{namespace}generator')
        if generator_elem is None:
            generator_elem = ET.SubElement(root, f'{namespace}generator')
        generator_elem.text = 'YouTube RSS Filter for Feedly'
        generator_elem.set('uri', 'https://github.com/your-repo/youtube-rss-filter')
    
    def combine_feeds(self, channel_feeds: List[str]) -> str:
        """Combine multiple channel feeds into one optimized feed"""
        valid_feeds = [feed for feed in channel_feeds if feed]
        
        if not valid_feeds:
            return self.create_empty_feed()
        
        # If only one feed, return it directly
        if len(valid_feeds) == 1:
            return valid_feeds[0]
        
        try:
            # Create optimized combined feed
            root = ET.Element('{http://www.w3.org/2005/Atom}feed')
            root.set('xmlns', 'http://www.w3.org/2005/Atom')
            root.set('xmlns:media', 'http://search.yahoo.com/mrss/')
            
            # Add feed metadata optimized for Feedly
            title_elem = ET.SubElement(root, '{http://www.w3.org/2005/Atom}title')
            title_elem.text = 'Filtered YouTube Feed (No Shorts)'
            
            subtitle_elem = ET.SubElement(root, '{http://www.w3.org/2005/Atom}subtitle')
            subtitle_elem.text = f'Combined feed from {len(CHANNELS) + len(CHANNEL_USERNAMES)} channels, optimized for Feedly'
            
            id_elem = ET.SubElement(root, '{http://www.w3.org/2005/Atom}id')
            id_elem.text = 'urn:uuid:filtered-youtube-feed'
            
            updated_elem = ET.SubElement(root, '{http://www.w3.org/2005/Atom}updated')
            updated_elem.text = datetime.now(timezone.utc).isoformat()
            
            # Add author info
            author_elem = ET.SubElement(root, '{http://www.w3.org/2005/Atom}author')
            author_name_elem = ET.SubElement(author_elem, '{http://www.w3.org/2005/Atom}name')
            author_name_elem.text = 'YouTube RSS Filter'
            
            # Add generator
            generator_elem = ET.SubElement(root, '{http://www.w3.org/2005/Atom}generator')
            generator_elem.text = 'YouTube RSS Filter for Feedly'
            
            # Add icon for better Feedly display
            icon_elem = ET.SubElement(root, '{http://www.w3.org/2005/Atom}icon')
            icon_elem.text = 'https://www.youtube.com/favicon.ico'
            
            namespace = '{http://www.w3.org/2005/Atom}'
            
            # Collect all entries
            all_entries = []
            for feed_content in valid_feeds:
                try:
                    feed_root = ET.fromstring(feed_content)
                    entries = feed_root.findall(f'{namespace}entry')
                    all_entries.extend(entries)
                except ET.ParseError as e:
                    logger.error(f"Error parsing feed for combination: {e}")
                    continue
            
            # Remove duplicates and sort
            seen_ids = set()
            unique_entries = []
            
            for entry in all_entries:
                # Try multiple ways to get unique identifier
                video_id_elem = entry.find('{http://search.yahoo.com/mrss/}videoId')
                entry_id_elem = entry.find(f'{namespace}id')
                
                unique_id = None
                if video_id_elem is not None and video_id_elem.text:
                    unique_id = video_id_elem.text
                elif entry_id_elem is not None and entry_id_elem.text:
                    unique_id = entry_id_elem.text
                else:
                    # Fallback to link
                    link_elem = entry.find(f'{namespace}link')
                    if link_elem is not None:
                        unique_id = link_elem.get('href')
                
                if unique_id and unique_id not in seen_ids:
                    seen_ids.add(unique_id)
                    unique_entries.append(entry)
                elif not unique_id:
                    # Include anyway if we can't determine uniqueness
                    unique_entries.append(entry)
            
            # Sort by published date (newest first)
            def get_published_date(entry):
                published_elem = entry.find(f'{namespace}published')
                if published_elem is not None and published_elem.text:
                    try:
                        return datetime.fromisoformat(published_elem.text.replace('Z', '+00:00'))
                    except:
                        pass
                return datetime.min.replace(tzinfo=timezone.utc)
            
            unique_entries.sort(key=get_published_date, reverse=True)
            
            # Add entries to the new root
            for entry in unique_entries:
                root.append(entry)
            
            logger.info(f"Combined feed created with {len(unique_entries)} total entries")
            return ET.tostring(root, encoding='unicode')
        
        except Exception as e:
            logger.error(f"Error combining feeds: {e}")
            return self.create_empty_feed()
    
    def create_empty_feed(self) -> str:
        """Create an empty RSS feed optimized for Feedly"""
        now = datetime.now(timezone.utc).isoformat()
        
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
    <title>Filtered YouTube Feed (No Shorts)</title>
    <subtitle>YouTube feed with Shorts filtered out, optimized for Feedly</subtitle>
    <id>urn:uuid:filtered-youtube-feed</id>
    <updated>{now}</updated>
    <generator uri="https://github.com/your-repo/youtube-rss-filter">YouTube RSS Filter for Feedly</generator>
    <icon>https://www.youtube.com/favicon.ico</icon>
    <author>
        <name>YouTube RSS Filter</name>
    </author>
</feed>'''
    
    def process_channel(self, channel_info: Tuple[Optional[str], Optional[str]]) -> Optional[str]:
        """Process a single channel with comprehensive error handling"""
        channel_id, username = channel_info
        
        try:
            rss_url = self.get_channel_rss_url(channel_id=channel_id, username=username)
            rss_content = self.fetch_rss_feed(rss_url)
            
            if rss_content:
                filtered_content = self.filter_shorts_from_feed(rss_content)
                STATS['channels_processed'] += 1
                return filtered_content
            
        except Exception as e:
            logger.error(f"Error processing channel {channel_id or username}: {e}")
        
        return None
    
    def get_filtered_feed(self) -> str:
        """Get filtered RSS feed from all configured channels"""
        start_time = time.time()
        STATS['total_requests'] += 1
        
        # Prepare channel list
        channel_list = []
        for channel_id in CHANNELS:
            if channel_id.strip():
                channel_list.append((channel_id.strip(), None))
        for username in CHANNEL_USERNAMES:
            if username.strip():
                channel_list.append((None, username.strip()))
        
        if not channel_list:
            logger.warning("No channels configured")
            return self.create_empty_feed()
        
        logger.info(f"Processing {len(channel_list)} channels")
        
        # Process channels in parallel
        channel_feeds = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_channel = {
                executor.submit(self.process_channel, channel_info): channel_info 
                for channel_info in channel_list
            }
            
            for future in concurrent.futures.as_completed(future_to_channel):
                channel_info = future_to_channel[future]
                try:
                    result = future.result()
                    if result:
                        channel_feeds.append(result)
                except Exception as e:
                    logger.error(f"Error processing channel {channel_info}: {e}")
        
        # Combine all feeds
        combined_feed = self.combine_feeds(channel_feeds)
        STATS['last_updated'] = datetime.now(timezone.utc).isoformat()
        
        total_time = time.time() - start_time
        logger.info(f"Feed generation completed in {total_time:.2f} seconds")
        
        return combined_feed

# Initialize the optimizer
rss_optimizer = YouTubeRSSOptimizer()

# Routes
@app.route('/rss')
@app.route('/feed')
@app.route('/atom')
def serve_rss():
    """Serve the filtered RSS feed with proper headers for Feedly"""
    try:
        filtered_feed = rss_optimizer.get_filtered_feed()
        
        response = Response(filtered_feed, mimetype='application/atom+xml')
        response.headers['Content-Type'] = 'application/atom+xml; charset=utf-8'
        response.headers['Cache-Control'] = 'public, max-age=1800'  # 30 minutes cache
        response.headers['Last-Modified'] = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        response.headers['ETag'] = f'"{hash(filtered_feed)}"'
        
        return response
    except Exception as e:
        logger.error(f"Error serving RSS: {e}")
        error_feed = rss_optimizer.create_empty_feed()
        return Response(error_feed, mimetype='application/atom+xml')

@app.route('/health')
def health_check():
    """Health check endpoint for Render"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'channels_configured': len(CHANNELS) + len(CHANNEL_USERNAMES),
        'version': '2.0.0'
    })

@app.route('/stats')
def statistics():
    """Statistics endpoint"""
    uptime = datetime.now(timezone.utc) - datetime.fromisoformat(STATS.get('last_updated', datetime.now(timezone.utc).isoformat()).replace('Z', '+00:00')) if STATS.get('last_updated') else timedelta(0)
    
    stats_copy = STATS.copy()
    stats_copy['uptime_seconds'] = uptime.total_seconds()
    stats_copy['avg_processing_time'] = stats_copy['processing_time'] / max(stats_copy['total_requests'], 1)
    stats_copy['filter_efficiency'] = (stats_copy['total_shorts_filtered'] / max(stats_copy['total_entries_processed'], 1)) * 100
    
    return jsonify(stats_copy)

@app.route('/debug')
def debug_info():
    """Debug endpoint for troubleshooting"""
    if not DEBUG_MODE:
        return jsonify({'error': 'Debug mode disabled'}), 403
    
    debug_data = {
        'configuration': {
            'channels': [ch for ch in CHANNELS if ch.strip()],
            'usernames': [un for un in CHANNEL_USERNAMES if un.strip()],
            'filter_config': FILTER_CONFIG,
            'has_youtube_api': bool(rss_optimizer.yt_api_key)
        },
        'statistics': STATS,
        'environment': {
            'port': PORT,
            'debug_mode': DEBUG_MODE
        }
    }
    
    return jsonify(debug_data)

@app.route('/')
def index():
    """Enhanced index page"""
    template = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>YouTube RSS Filter for Feedly</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            .header { text-align: center; margin-bottom: 30px; }
            .card { border: 1px solid #ddd; padding: 20px; margin: 20px 0; border-radius: 8px; }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
            .stat-item { background: #f5f5f5; padding: 10px; border-radius: 4px; text-align: center; }
            .links { display: flex; gap: 15px; flex-wrap: wrap; }
            .link-button { 
                display: inline-block; padding: 10px 15px; background: #007cba; color: white; 
                text-decoration: none; border-radius: 5px; transition: background 0.3s; 
            }
            .link-button:hover { background: #005a87; }
            .feedly-specific { background: #f0f8ff; border-left: 4px solid #007cba; }
            .config-item { margin: 10px 0; }
            .badge { 
                display: inline-block; padding: 2px 8px; background: #e0e0e0; 
                border-radius: 12px; font-size: 0.9em; 
            }
            .success { background: #d4edda; color: #155724; }
            .warning { background: #fff3cd; color: #856404; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🎥 YouTube RSS Filter for Feedly</h1>
            <p>Filter out YouTube Shorts and optimize feeds for RSS readers</p>
        </div>
        
        <div class="card feedly-specific">
            <h2>📰 For Feedly Users</h2>
            <p>Add this URL to Feedly to get a clean feed without Shorts:</p>
            <div style="background: white; padding: 10px; border-radius: 4px; font-family: monospace; word-break: break-all;">
                {{ request.url_root }}rss
            </div>
            <p><small>💡 Tip: The feed includes video duration in titles and is optimized for Feedly's interface</small></p>
        </div>
        
        <div class="card">
            <h2>🔗 Available Endpoints</h2>
            <div class="links">
                <a href="/rss" class="link-button">📡 RSS Feed</a>
                <a href="/feed" class="link-button">📄 Atom Feed</a>
                <a href="/stats" class="link-button">📊 Statistics</a>
                <a href="/health" class="link-button">❤️ Health Check</a>
                {% if debug_mode %}
                <a href="/debug" class="link-button">🔍 Debug Info</a>
                {% endif %}
            </div>
        </div>
        
        <div class="card">
            <h2>📈 Live Statistics</h2>
            <div class="stats">
                <div class="stat-item">
                    <strong>Total Requests</strong><br>
                    <span style="font-size: 1.5em;">{{ stats.total_requests }}</span>
                </div>
                <div class="stat-item">
                    <strong>Shorts Filtered</strong><br>
                    <span style="font-size: 1.5em; color: #dc3545;">{{ stats.total_shorts_filtered }}</span>
                </div>
                <div class="stat-item">
                    <strong>Videos Processed</strong><br>
                    <span style="font-size: 1.5em; color: #28a745;">{{ stats.total_entries_processed }}</span>
                </div>
                <div class="stat-item">
                    <strong>Channels Active</strong><br>
                    <span style="font-size: 1.5em;">{{ channels_count }}</span>
                </div>
            </div>
            {% if stats.total_entries_processed > 0 %}
            <p style="text-align: center; margin-top: 15px;">
                <span class="badge success">
                    Filter Efficiency: {{ "%.1f"|format((stats.total_shorts_filtered / stats.total_entries_processed) * 100) }}%
                </span>
            </p>
            {% endif %}
        </div>
        
        <div class="card">
            <h2>⚙️ Configuration</h2>
            <div class="config-item">
                <strong>Configured Channels:</strong> 
                <span class="badge">{{ channels_count }} channels</span>
            </div>
            <div class="config-item">
                <strong>Max Short Duration:</strong> 
                <span class="badge">{{ filter_config.max_short_duration }}s</span>
            </div>
            <div class="config-item">
                <strong>Duration in Titles:</strong> 
                <span class="badge {{ 'success' if filter_config.include_duration_in_title else 'warning' }}">
                    {{ 'Enabled' if filter_config.include_duration_in_title else 'Disabled' }}
                </span>
            </div>
            <div class="config-item">
                <strong>Strict Filtering:</strong> 
                <span class="badge {{ 'warning' if filter_config.strict_mode else 'success' }}">
                    {{ 'Enabled' if filter_config.strict_mode else 'Disabled' }}
                </span>
            </div>
            <div class="config-item">
                <strong>YouTube API:</strong> 
                <span class="badge {{ 'success' if has_youtube_api else 'warning' }}">
                    {{ 'Connected' if has_youtube_api else 'Not configured' }}
                </span>
            </div>
        </div>
        
        <div class="card">
            <h2>🚀 Deployment on Render</h2>
            <p>This service is ready for deployment on Render with these environment variables:</p>
            <ul>
                <li><code>YOUTUBE_CHANNELS</code> - Comma-separated channel IDs</li>
                <li><code>YOUTUBE_USERNAMES</code> - Comma-separated usernames</li>
                <li><code>YOUTUBE_API_KEY</code> - Optional, for exact duration lookup</li>
                <li><code>MAX_SHORT_DURATION</code> - Max duration for Shorts (default: 90s)</li>
                <li><code>INCLUDE_DURATION</code> - Add duration to titles (default: true)</li>
                <li><code>STRICT_FILTER</code> - Enable strict filtering (default: false)</li>
            </ul>
        </div>
        
        <div class="card">
            <h2>💡 Features for RSS Readers</h2>
            <ul>
                <li>✅ Video duration in titles for easy scanning</li>
                <li>✅ Enhanced metadata for better categorization</li>
                <li>✅ Optimized for Feedly's interface</li>
                <li>✅ Proper caching headers for performance</li>
                <li>✅ Duplicate removal across channels</li>
                <li>✅ Chronological sorting (newest first)</li>
                <li>✅ Multiple detection methods for Shorts</li>
                <li>✅ Statistics and monitoring</li>
            </ul>
        </div>
        
        <footer style="text-align: center; margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee;">
            <p><small>Last updated: {{ stats.last_updated or 'Never' }}</small></p>
        </footer>
    </body>
    </html>
    '''
    
    return render_template_string(template, 
        stats=STATS,
        channels_count=len([ch for ch in CHANNELS if ch.strip()]) + len([un for un in CHANNEL_USERNAMES if un.strip()]),
        filter_config=FILTER_CONFIG,
        has_youtube_api=bool(rss_optimizer.yt_api_key),
        debug_mode=DEBUG_MODE
    )

if __name__ == '__main__':
    # Ensure we have some configuration
    if not CHANNELS and not CHANNEL_USERNAMES:
        logger.warning("No channels configured! Set YOUTUBE_CHANNELS or YOUTUBE_USERNAMES environment variables")
    
    logger.info(f"Starting YouTube RSS Filter for Feedly on port {PORT}")
    logger.info(f"Configured channels: {len(CHANNELS)} IDs, {len(CHANNEL_USERNAMES)} usernames")
    logger.info(f"Filter settings: max_duration={FILTER_CONFIG['max_short_duration']}s, strict_mode={FILTER_CONFIG['strict_mode']}")
    
    app.run(host='0.0.0.0', port=PORT, debug=DEBUG_MODE)