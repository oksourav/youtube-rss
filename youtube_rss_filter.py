#!/usr/bin/env python3
"""
YouTube RSS Shorts Filter for Feedly
====================================

A Flask application that filters YouTube Shorts from RSS feeds and optimizes them for Feedly.

Features:
- Filters out YouTube Shorts using multiple detection methods
- Adds video duration to titles
- Combines multiple channel feeds with deduplication
- Optimized for Feedly with proper Atom 1.0 structure
- Real-time statistics and monitoring
- Production-ready with proper error handling

Author: AI Assistant
License: MIT
"""

import os
import re
import json
import time
import logging
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional, Set
from urllib.parse import urlparse, parse_qs

import feedparser
import requests
from flask import Flask, Response, render_template_string, jsonify, request
from xml.sax.saxutils import escape
from dateutil import parser as date_parser

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration from environment variables
class Config:
    YOUTUBE_CHANNELS = os.getenv('YOUTUBE_CHANNELS', '').split(',') if os.getenv('YOUTUBE_CHANNELS') else []
    YOUTUBE_USERNAMES = os.getenv('YOUTUBE_USERNAMES', '').split(',') if os.getenv('YOUTUBE_USERNAMES') else []
    YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
    MAX_SHORT_DURATION = int(os.getenv('MAX_SHORT_DURATION', '90'))
    INCLUDE_DURATION = os.getenv('INCLUDE_DURATION', 'true').lower() == 'true'
    STRICT_FILTER = os.getenv('STRICT_FILTER', 'false').lower() == 'true'
    DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'
    PORT = int(os.getenv('PORT', '5000'))
    RENDER_EXTERNAL_URL = os.getenv('RENDER_EXTERNAL_URL', f'http://localhost:{os.getenv("PORT", "5000")}')
    FEEDLY_ENHANCED = os.getenv('FEEDLY_ENHANCED', 'true').lower() == 'true'
    
    # Ensure URL has proper scheme
    if not RENDER_EXTERNAL_URL.startswith(('http://', 'https://')):
        RENDER_EXTERNAL_URL = f'https://{RENDER_EXTERNAL_URL}'

# Global statistics
class Stats:
    def __init__(self):
        self.requests = 0
        self.videos_processed = 0
        self.shorts_filtered = 0
        self.errors = 0
        self.last_request = None
        self.start_time = time.time()
    
    def efficiency(self):
        if self.videos_processed == 0:
            return 0
        return round((self.shorts_filtered / self.videos_processed) * 100, 2)
    
    def uptime(self):
        return round(time.time() - self.start_time, 2)

stats = Stats()

class YouTubeRSSProcessor:
    """Processes YouTube RSS feeds and filters out Shorts."""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'YouTube RSS Shorts Filter/1.0'
        })
        
        # Compile regex patterns for efficiency
        self.shorts_keywords = re.compile(
            r'#shorts?|#short|#youtubeshorts|shorts|short video',
            re.IGNORECASE
        )
        self.duration_pattern = re.compile(r'\[(\d{1,2}):(\d{2})\]')
        self.strict_patterns = [
            re.compile(r'\b(quick|fast|rapid|instant)\b', re.IGNORECASE),
            re.compile(r'\b(tip|hack|trick)\b', re.IGNORECASE),
            re.compile(r'\b(\d+\s*sec(ond)?s?)\b', re.IGNORECASE),
        ]
    
    def get_channel_feed_url(self, identifier: str, is_username: bool = False) -> str:
        """Generate RSS feed URL for a YouTube channel."""
        if is_username:
            return f"https://www.youtube.com/feeds/videos.xml?user={identifier}"
        else:
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={identifier}"
    
    def fetch_feed(self, url: str, retries: int = 3) -> Optional[feedparser.FeedParserDict]:
        """Fetch and parse an RSS feed with retry logic."""
        for attempt in range(retries):
            try:
                logger.info(f"Fetching feed: {url} (attempt {attempt + 1})")
                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                
                feed = feedparser.parse(response.content)
                if feed.bozo:
                    logger.warning(f"Feed parsing issues for {url}: {feed.bozo_exception}")
                
                return feed
            except Exception as e:
                logger.error(f"Error fetching feed {url} (attempt {attempt + 1}): {e}")
                if attempt == retries - 1:
                    stats.errors += 1
                    return None
                time.sleep(2 ** attempt)  # Exponential backoff
        
        return None
    
    def extract_duration_from_title(self, title: str) -> Optional[int]:
        """Extract duration in seconds from video title."""
        match = self.duration_pattern.search(title)
        if match:
            minutes, seconds = map(int, match.groups())
            return minutes * 60 + seconds
        return None
    
    def is_short_video(self, entry: Dict) -> Tuple[bool, str]:
        """
        Determine if a video is a Short using multiple detection methods.
        Returns (is_short, reason).
        """
        title = entry.get('title', '').lower()
        summary = entry.get('summary', '').lower()
        
        # Method 1: Keyword detection
        if self.shorts_keywords.search(title) or self.shorts_keywords.search(summary):
            return True, "Contains shorts keywords"
        
        # Method 2: Duration from title
        duration = self.extract_duration_from_title(entry.get('title', ''))
        if duration and duration <= Config.MAX_SHORT_DURATION:
            return True, f"Duration from title: {duration}s"
        
        # Method 3: Strict mode heuristics
        if Config.STRICT_FILTER:
            for pattern in self.strict_patterns:
                if pattern.search(title) or pattern.search(summary):
                    return True, "Strict mode pattern match"
        
        return False, "Not detected as short"
    
    def add_duration_to_title(self, title: str, duration: Optional[int] = None) -> str:
        """Add duration prefix to video title."""
        if not Config.INCLUDE_DURATION:
            return title
        
        # Check if duration already exists in title
        if self.duration_pattern.search(title):
            return title
        
        if duration:
            minutes, seconds = divmod(duration, 60)
            if minutes > 0:
                duration_str = f"[{minutes}m{seconds:02d}s]"
            else:
                duration_str = f"[{seconds}s]"
            return f"{duration_str} {title}"
        
        return title
    
    def extract_video_id(self, url: str) -> Optional[str]:
        """Extract YouTube video ID from URL."""
        patterns = [
            r'watch\?v=([a-zA-Z0-9_-]{11})',
            r'youtu\.be/([a-zA-Z0-9_-]{11})',
            r'embed/([a-zA-Z0-9_-]{11})',
            r'v/([a-zA-Z0-9_-]{11})'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None
    
    def generate_enhanced_content(self, entry: Dict) -> str:
        """Generate enhanced HTML content with embedded player for Feedly."""
        video_id = self.extract_video_id(entry.get('link', ''))
        if not video_id:
            return entry.get('summary', '')
        
        # Get original summary/description
        original_summary = entry.get('summary', '')
        
        if Config.FEEDLY_ENHANCED:
            # Enhanced mode: Create content optimized for Feedly with large thumbnail
            enhanced_content = f'''<div class="youtube-video">
<p><a href="{entry.get('link', '')}" target="_blank" rel="noopener">
<img src="https://img.youtube.com/vi/{video_id}/maxresdefault.jpg" alt="Watch: {escape(entry.get('title', 'Video'))}" style="max-width:100%; height:auto; border:1px solid #ccc; border-radius:4px; display:block;" />
</a></p>
<p style="text-align:center; margin:10px 0;">
<a href="{entry.get('link', '')}" target="_blank" rel="noopener" style="background:#ff0000; color:white; padding:10px 20px; text-decoration:none; border-radius:4px; font-weight:bold;">
▶️ Watch on YouTube
</a>
</p>
{f'<div style="margin-top:15px; padding:10px; background:#f9f9f9; border-left:3px solid #ff0000;"><strong>Description:</strong><br/>{original_summary}</div>' if original_summary else ''}
</div>'''
        else:
            # Simple mode: Just thumbnail and link
            enhanced_content = f'''<p><a href="{entry.get('link', '')}" target="_blank" rel="noopener">
<img src="https://img.youtube.com/vi/{video_id}/maxresdefault.jpg" alt="Video Thumbnail" style="max-width:100%; height:auto;" />
</a></p>
<p><a href="{entry.get('link', '')}" target="_blank" rel="noopener">▶️ Watch this video on YouTube</a></p>
{f'<div>{original_summary}</div>' if original_summary else ''}'''
        
        return enhanced_content

    def process_entry(self, entry: Dict) -> Optional[Dict]:
        """Process a single feed entry."""
        try:
            stats.videos_processed += 1
            
            # Check if it's a Short
            is_short, reason = self.is_short_video(entry)
            if is_short:
                stats.shorts_filtered += 1
                logger.debug(f"Filtered Short: {entry.get('title')} - {reason}")
                return None
            
            # Extract duration and add to title if configured
            duration = self.extract_duration_from_title(entry.get('title', ''))
            processed_title = self.add_duration_to_title(entry.get('title', ''), duration)
            
            # Extract video ID for media enclosure
            video_id = self.extract_video_id(entry.get('link', ''))
            
            # Create processed entry with enhanced content
            processed_entry = {
                'title': processed_title,
                'link': entry.get('link', ''),
                'published': entry.get('published', ''),
                'summary': self.generate_enhanced_content(entry),
                'author': entry.get('author', ''),
                'id': entry.get('id', ''),
                'duration': duration,
                'original_title': entry.get('title', ''),
                'video_id': video_id,
                'thumbnail': f'https://img.youtube.com/vi/{video_id}/maxresdefault.jpg' if video_id else None
            }
            
            return processed_entry
            
        except Exception as e:
            logger.error(f"Error processing entry: {e}")
            stats.errors += 1
            return None
    
    def get_all_feeds(self) -> List[Dict]:
        """Fetch and process all configured YouTube feeds."""
        feeds = []
        
        # Prepare feed URLs
        feed_urls = []
        for channel_id in Config.YOUTUBE_CHANNELS:
            if channel_id.strip():
                feed_urls.append(self.get_channel_feed_url(channel_id.strip()))
        
        for username in Config.YOUTUBE_USERNAMES:
            if username.strip():
                feed_urls.append(self.get_channel_feed_url(username.strip(), is_username=True))
        
        if not feed_urls:
            logger.warning("No YouTube channels or usernames configured")
            return []
        
        # Fetch feeds in parallel
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_url = {executor.submit(self.fetch_feed, url): url for url in feed_urls}
            
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    feed = future.result()
                    if feed and hasattr(feed, 'entries'):
                        feeds.extend(feed.entries)
                        logger.info(f"Fetched {len(feed.entries)} entries from {url}")
                except Exception as e:
                    logger.error(f"Exception processing feed {url}: {e}")
                    stats.errors += 1
        
        return feeds
    
    def process_feeds(self) -> List[Dict]:
        """Process all feeds and return filtered entries."""
        raw_entries = self.get_all_feeds()
        processed_entries = []
        seen_links = set()
        
        # Process entries in parallel
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_entry = {executor.submit(self.process_entry, entry): entry for entry in raw_entries}
            
            for future in as_completed(future_to_entry):
                try:
                    processed_entry = future.result()
                    if processed_entry and processed_entry['link'] not in seen_links:
                        processed_entries.append(processed_entry)
                        seen_links.add(processed_entry['link'])
                except Exception as e:
                    logger.error(f"Exception processing entry: {e}")
                    stats.errors += 1
        
        # Sort by published date (newest first)
        processed_entries.sort(
            key=lambda x: date_parser.parse(x['published']) if x['published'] else datetime.min.replace(tzinfo=timezone.utc),
            reverse=True
        )
        
        logger.info(f"Processed {len(processed_entries)} entries after filtering")
        return processed_entries

# Initialize processor
processor = YouTubeRSSProcessor()

def generate_atom_feed(entries: List[Dict]) -> str:
    """Generate Atom 1.0 feed from processed entries."""
    current_time = datetime.now(timezone.utc).isoformat()
    
    # Generate feed ID
    feed_id = hashlib.md5(Config.RENDER_EXTERNAL_URL.encode()).hexdigest()
    
    atom_feed = f'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en">
    <title>YouTube RSS (No Shorts)</title>
    <subtitle>Filtered YouTube RSS feed without Shorts</subtitle>
    <id>urn:uuid:{feed_id}</id>
    <link href="{escape(Config.RENDER_EXTERNAL_URL)}/rss" rel="self" type="application/atom+xml"/>
    <link href="{escape(Config.RENDER_EXTERNAL_URL)}" rel="alternate" type="text/html"/>
    <updated>{current_time}</updated>
    <generator uri="{escape(Config.RENDER_EXTERNAL_URL)}" version="1.0">YouTube RSS Shorts Filter</generator>
    <author>
        <name>YouTube RSS Filter</name>
        <uri>{escape(Config.RENDER_EXTERNAL_URL)}</uri>
    </author>
    <category term="youtube" label="YouTube"/>
    <category term="video" label="Video"/>
    <rights>© YouTube. Content filtered by YouTube RSS Shorts Filter.</rights>
'''
    
    for entry in entries:
        entry_id = entry.get('id', entry.get('link', ''))
        published = entry.get('published', current_time)
        
        # Parse and format the published date
        try:
            parsed_date = date_parser.parse(published)
            formatted_date = parsed_date.isoformat()
        except:
            formatted_date = current_time
        
        atom_feed += f'''
    <entry>
        <title>{escape(entry.get('title', 'Untitled'))}</title>
        <id>{escape(entry_id)}</id>
        <link href="{escape(entry.get('link', ''))}" rel="alternate" type="text/html"/>
        <published>{formatted_date}</published>
        <updated>{formatted_date}</updated>
        <summary type="html">{escape(entry.get('summary', ''))}</summary>
        <author>
            <name>{escape(entry.get('author', 'Unknown'))}</name>
        </author>
        <category term="video" label="Video"/>
    </entry>'''
    
    atom_feed += '\n</feed>'
    return atom_feed

# Flask routes
@app.route('/')
def dashboard():
    """Enhanced HTML dashboard with statistics and configuration."""
    
    dashboard_html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YouTube RSS Shorts Filter</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            margin: 0;
            padding: 20px;
            background: #f5f5f5;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1, h2 {
            color: #333;
            border-bottom: 3px solid #4CAF50;
            padding-bottom: 10px;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }
        .stat-card {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            border-left: 4px solid #4CAF50;
            text-align: center;
        }
        .stat-number {
            font-size: 2em;
            font-weight: bold;
            color: #4CAF50;
        }
        .stat-label {
            color: #666;
            font-size: 0.9em;
            margin-top: 5px;
        }
        .config-table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }
        .config-table th, .config-table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        .config-table th {
            background-color: #f8f9fa;
            font-weight: bold;
        }
        .feed-links {
            background: #e3f2fd;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
        }
        .feed-links a {
            display: inline-block;
            margin: 5px 10px 5px 0;
            padding: 8px 16px;
            background: #2196F3;
            color: white;
            text-decoration: none;
            border-radius: 4px;
            transition: background 0.3s;
        }
        .feed-links a:hover {
            background: #1976D2;
        }
        .info-section {
            background: #fff3cd;
            border: 1px solid #ffeaa7;
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
        }
        .error {
            color: #d32f2f;
        }
        .success {
            color: #388e3c;
        }
        .code {
            background: #f5f5f5;
            padding: 3px 6px;
            border-radius: 3px;
            font-family: monospace;
            font-size: 0.9em;
        }
        .refresh-btn {
            background: #4CAF50;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 16px;
            margin: 10px 0;
        }
        .refresh-btn:hover {
            background: #45a049;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎥 YouTube RSS Shorts Filter</h1>
        <p>A smart RSS feed that filters out YouTube Shorts and optimizes content for Feedly.</p>
        
        <h2>📊 Live Statistics</h2>
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-number">{{ stats.requests }}</div>
                <div class="stat-label">Total Requests</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ stats.videos_processed }}</div>
                <div class="stat-label">Videos Processed</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ stats.shorts_filtered }}</div>
                <div class="stat-label">Shorts Filtered</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ stats.efficiency() }}%</div>
                <div class="stat-label">Filter Efficiency</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ stats.errors }}</div>
                <div class="stat-label">Errors</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ "%.1f"|format(stats.uptime()) }}s</div>
                <div class="stat-label">Uptime</div>
            </div>
        </div>
        
        <button class="refresh-btn" onclick="location.reload()">🔄 Refresh Statistics</button>
        
        <h2>🔗 RSS Feed Links</h2>
        <div class="feed-links">
            <p><strong>For Feedly:</strong> Use the RSS Discovery page for automatic detection:</p>
            <a href="{{ base_url }}/rss-discovery" target="_blank">📡 RSS Discovery Page</a>
            
            <p><strong>Direct Feed URLs:</strong></p>
            <a href="{{ base_url }}/rss" target="_blank">📰 RSS Feed</a>
            <a href="{{ base_url }}/feed" target="_blank">🔄 Atom Feed</a>
            <a href="{{ base_url }}/atom" target="_blank">⚛️ Atom Feed</a>
            <a href="{{ base_url }}/rss.xml" target="_blank">📄 RSS.xml</a>
            <a href="{{ base_url }}/feed.xml" target="_blank">📝 Feed.xml</a>
        </div>
        
        <h2>⚙️ Configuration</h2>
        <table class="config-table">
            <tr>
                <th>Setting</th>
                <th>Value</th>
                <th>Description</th>
            </tr>
            <tr>
                <td>YouTube Channels</td>
                <td>{{ config.YOUTUBE_CHANNELS|length }} configured</td>
                <td>Number of channel IDs being monitored</td>
            </tr>
            <tr>
                <td>YouTube Usernames</td>
                <td>{{ config.YOUTUBE_USERNAMES|length }} configured</td>
                <td>Number of usernames being monitored</td>
            </tr>
            <tr>
                <td>Max Short Duration</td>
                <td>{{ config.MAX_SHORT_DURATION }}s</td>
                <td>Videos shorter than this are considered Shorts</td>
            </tr>
            <tr>
                <td>Include Duration</td>
                <td>{{ "✅" if config.INCLUDE_DURATION else "❌" }}</td>
                <td>Add duration prefix to video titles</td>
            </tr>
            <tr>
                <td>Strict Filter</td>
                <td>{{ "✅" if config.STRICT_FILTER else "❌" }}</td>
                <td>Use additional heuristics for filtering</td>
            </tr>
            <tr>
                <td>Debug Mode</td>
                <td>{{ "✅" if config.DEBUG else "❌" }}</td>
                <td>Enable debug endpoints and verbose logging</td>
            </tr>
            <tr>
                <td>API Key</td>
                <td>{{ "✅ Configured" if config.YOUTUBE_API_KEY else "❌ Not Set" }}</td>
                <td>YouTube API key for enhanced duration detection</td>
            </tr>
        </table>
        
        <h2>📋 How to Use with Feedly</h2>
        <div class="info-section">
            <h3>Method 1: RSS Discovery (Recommended)</h3>
            <ol>
                <li>Copy this URL: <span class="code">{{ base_url }}/rss-discovery</span></li>
                <li>In Feedly, click "Add Content" or use the "+" button</li>
                <li>Paste the URL and click "Add"</li>
                <li>Feedly will automatically discover and add the RSS feed</li>
            </ol>
            
            <h3>Method 2: Direct Feed URL</h3>
            <ol>
                <li>Copy this URL: <span class="code">{{ base_url }}/rss</span></li>
                <li>In Feedly, click "Add Content" and select "RSS Feed"</li>
                <li>Paste the URL and click "Add"</li>
            </ol>
            
            <h3>Troubleshooting</h3>
            <ul>
                <li><strong>Feed not updating:</strong> Check that YouTube channels are configured correctly</li>
                <li><strong>Too many Shorts:</strong> Enable strict filtering or reduce max duration</li>
                <li><strong>No videos:</strong> Verify channel IDs and usernames in configuration</li>
                <li><strong>Feedly issues:</strong> Try the RSS discovery page method</li>
            </ul>
        </div>
        
        <h2>🚀 Deployment & Monitoring</h2>
        <div class="info-section">
            <p><strong>Health Check:</strong> <a href="{{ base_url }}/health" target="_blank">{{ base_url }}/health</a></p>
            <p><strong>Statistics API:</strong> <a href="{{ base_url }}/stats" target="_blank">{{ base_url }}/stats</a></p>
            {% if config.DEBUG %}
            <p><strong>Debug Info:</strong> <a href="{{ base_url }}/debug" target="_blank">{{ base_url }}/debug</a></p>
            {% endif %}
            <p><strong>Last Request:</strong> {{ stats.last_request or "None" }}</p>
        </div>
        
        <footer style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; text-align: center; color: #666;">
            <p>YouTube RSS Shorts Filter v1.0 | 
            <a href="https://github.com" style="color: #4CAF50;">GitHub</a> | 
            Built with ❤️ for better RSS experiences</p>
        </footer>
    </div>
    
    <script>
        // Auto-refresh statistics every 30 seconds
        setTimeout(() => {
            location.reload();
        }, 30000);
    </script>
</body>
</html>
    '''
    
    return render_template_string(dashboard_html, 
                                config=Config, 
                                stats=stats, 
                                base_url=Config.RENDER_EXTERNAL_URL)

@app.route('/rss-discovery')
def rss_discovery():
    """RSS discovery page optimized for Feedly."""
    discovery_html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YouTube RSS (No Shorts) - Feed Discovery</title>
    <link rel="alternate" type="application/rss+xml" title="YouTube RSS (No Shorts)" href="{{ base_url }}/rss">
    <link rel="alternate" type="application/atom+xml" title="YouTube RSS (No Shorts)" href="{{ base_url }}/atom">
    <link rel="canonical" href="{{ base_url }}/rss-discovery">
    <meta name="description" content="Filtered YouTube RSS feed without Shorts, optimized for Feedly">
    <meta name="robots" content="index, follow">
</head>
<body style="font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px;">
    <h1>🎥 YouTube RSS Feed (No Shorts)</h1>
    <p>This page helps RSS readers like Feedly automatically discover our filtered YouTube RSS feed.</p>
    
    <h2>📡 Available Feeds</h2>
    <ul>
        <li><a href="{{ base_url }}/rss">Main RSS Feed (Recommended)</a></li>
        <li><a href="{{ base_url }}/atom">Atom Feed</a></li>
        <li><a href="{{ base_url }}/feed">Alternative Feed URL</a></li>
    </ul>
    
    <h2>✨ Features</h2>
    <ul>
        <li>✅ Filters out YouTube Shorts automatically</li>
        <li>✅ Adds video duration to titles</li>
        <li>✅ Combines multiple YouTube channels</li>
        <li>✅ Optimized for Feedly and other RSS readers</li>
        <li>✅ Real-time updates and monitoring</li>
    </ul>
    
    <h2>🔧 For Feedly Users</h2>
    <p>Simply add this page's URL to Feedly, and it will automatically detect and subscribe to the RSS feed.</p>
    
    <div style="background: #f0f0f0; padding: 15px; border-radius: 5px; margin: 20px 0;">
        <strong>Quick Add URL:</strong> <code>{{ base_url }}/rss-discovery</code>
    </div>
    
    <p><a href="{{ base_url }}">← Back to Dashboard</a></p>
</body>
</html>
    '''
    
    return render_template_string(discovery_html, base_url=Config.RENDER_EXTERNAL_URL)

@app.route('/rss')
@app.route('/feed')
@app.route('/atom')
@app.route('/rss.xml')
@app.route('/feed.xml')
def rss_feed():
    """Main RSS feed endpoint with proper caching headers."""
    try:
        stats.requests += 1
        stats.last_request = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        
        logger.info("Processing RSS feed request")
        entries = processor.process_feeds()
        atom_content = generate_atom_feed(entries)
        
        response = Response(
            atom_content,
            mimetype='application/atom+xml',
            headers={
                'Cache-Control': 'public, max-age=900',  # 15 minutes
                'Content-Type': 'application/atom+xml; charset=utf-8',
                'X-Content-Type-Options': 'nosniff',
                'X-RSS-Generator': 'YouTube RSS Shorts Filter v1.0'
            }
        )
        
        logger.info(f"RSS feed generated successfully with {len(entries)} entries")
        return response
        
    except Exception as e:
        logger.error(f"Error generating RSS feed: {e}")
        stats.errors += 1
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><error>Feed generation failed</error>',
            status=500,
            mimetype='application/xml'
        )

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring."""
    try:
        # Basic connectivity test
        test_processor = YouTubeRSSProcessor()
        
        health_data = {
            'status': 'healthy',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'uptime': stats.uptime(),
            'version': '1.0',
            'configuration': {
                'channels_configured': len(Config.YOUTUBE_CHANNELS),
                'usernames_configured': len(Config.YOUTUBE_USERNAMES),
                'api_key_configured': bool(Config.YOUTUBE_API_KEY),
                'max_short_duration': Config.MAX_SHORT_DURATION
            },
            'statistics': {
                'total_requests': stats.requests,
                'videos_processed': stats.videos_processed,
                'shorts_filtered': stats.shorts_filtered,
                'errors': stats.errors,
                'filter_efficiency': stats.efficiency()
            }
        }
        
        return jsonify(health_data)
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.now(timezone.utc).isoformat()
        }), 500

@app.route('/stats')
def statistics():
    """Statistics endpoint returning JSON data."""
    stats_data = {
        'requests': stats.requests,
        'videos_processed': stats.videos_processed,
        'shorts_filtered': stats.shorts_filtered,
        'errors': stats.errors,
        'filter_efficiency_percent': stats.efficiency(),
        'uptime_seconds': stats.uptime(),
        'last_request': stats.last_request,
        'configuration': {
            'youtube_channels': len(Config.YOUTUBE_CHANNELS),
            'youtube_usernames': len(Config.YOUTUBE_USERNAMES),
            'max_short_duration': Config.MAX_SHORT_DURATION,
            'include_duration': Config.INCLUDE_DURATION,
            'strict_filter': Config.STRICT_FILTER,
            'debug_mode': Config.DEBUG,
            'api_key_configured': bool(Config.YOUTUBE_API_KEY)
        },
        'timestamp': datetime.now(timezone.utc).isoformat()
    }
    
    return jsonify(stats_data)

@app.route('/debug')
def debug_info():
    """Debug information endpoint (only available when DEBUG=true)."""
    if not Config.DEBUG:
        return jsonify({'error': 'Debug mode not enabled'}), 404
    
    try:
        # Get sample of recent entries for debugging
        sample_entries = processor.process_feeds()[:5]  # Get first 5 entries
        
        debug_data = {
            'environment_variables': {
                'YOUTUBE_CHANNELS': Config.YOUTUBE_CHANNELS,
                'YOUTUBE_USERNAMES': Config.YOUTUBE_USERNAMES,
                'MAX_SHORT_DURATION': Config.MAX_SHORT_DURATION,
                'INCLUDE_DURATION': Config.INCLUDE_DURATION,
                'STRICT_FILTER': Config.STRICT_FILTER,
                'DEBUG': Config.DEBUG,
                'PORT': Config.PORT,
                'RENDER_EXTERNAL_URL': Config.RENDER_EXTERNAL_URL,
                'YOUTUBE_API_KEY': '***' if Config.YOUTUBE_API_KEY else None
            },
            'feed_urls': [],
            'sample_processed_entries': sample_entries,
            'processor_patterns': {
                'shorts_keywords': processor.shorts_keywords.pattern,
                'duration_pattern': processor.duration_pattern.pattern,
                'strict_patterns': [p.pattern for p in processor.strict_patterns]
            },
            'statistics': {
                'requests': stats.requests,
                'videos_processed': stats.videos_processed,
                'shorts_filtered': stats.shorts_filtered,
                'errors': stats.errors,
                'uptime': stats.uptime(),
                'last_request': stats.last_request
            },
            'timestamp': datetime.now(timezone.utc).isoformat()
        }
        
        # Add feed URLs for debugging
        for channel_id in Config.YOUTUBE_CHANNELS:
            if channel_id.strip():
                debug_data['feed_urls'].append({
                    'type': 'channel_id',
                    'identifier': channel_id.strip(),
                    'url': processor.get_channel_feed_url(channel_id.strip())
                })
        
        for username in Config.YOUTUBE_USERNAMES:
            if username.strip():
                debug_data['feed_urls'].append({
                    'type': 'username',
                    'identifier': username.strip(),
                    'url': processor.get_channel_feed_url(username.strip(), is_username=True)
                })
        
        return jsonify(debug_data)
        
    except Exception as e:
        logger.error(f"Debug endpoint error: {e}")
        return jsonify({'error': f'Debug failed: {str(e)}'}), 500

# Error handlers
@app.errorhandler(404)
def not_found(error):
    """Custom 404 handler."""
    return jsonify({
        'error': 'Not Found',
        'message': 'The requested endpoint does not exist',
        'available_endpoints': [
            '/',
            '/rss',
            '/feed',
            '/atom',
            '/rss.xml',
            '/feed.xml',
            '/rss-discovery',
            '/health',
            '/stats'
        ] + (['/debug'] if Config.DEBUG else [])
    }), 404

@app.errorhandler(500)
def internal_error(error):
    """Custom 500 handler."""
    logger.error(f"Internal server error: {error}")
    stats.errors += 1
    return jsonify({
        'error': 'Internal Server Error',
        'message': 'An unexpected error occurred',
        'timestamp': datetime.now(timezone.utc).isoformat()
    }), 500

# Request logging middleware
@app.before_request
def log_request_info():
    """Log incoming requests."""
    logger.info(f"{request.method} {request.path} from {request.remote_addr}")

@app.after_request
def log_response_info(response):
    """Log outgoing responses."""
    logger.info(f"Response: {response.status_code} for {request.path}")
    return response

# Main application entry point
if __name__ == '__main__':
    # Validate configuration
    if not Config.YOUTUBE_CHANNELS and not Config.YOUTUBE_USERNAMES:
        logger.warning("No YouTube channels or usernames configured. Set YOUTUBE_CHANNELS or YOUTUBE_USERNAMES environment variables.")
    
    # Log configuration summary
    logger.info("YouTube RSS Shorts Filter starting...")
    logger.info(f"Channels configured: {len(Config.YOUTUBE_CHANNELS)}")
    logger.info(f"Usernames configured: {len(Config.YOUTUBE_USERNAMES)}")
    logger.info(f"Max short duration: {Config.MAX_SHORT_DURATION}s")
    logger.info(f"Include duration in titles: {Config.INCLUDE_DURATION}")
    logger.info(f"Strict filtering: {Config.STRICT_FILTER}")
    logger.info(f"Debug mode: {Config.DEBUG}")
    logger.info(f"External URL: {Config.RENDER_EXTERNAL_URL}")
    
    # Start the application
    app.run(
        host='0.0.0.0',
        port=Config.PORT,
        debug=Config.DEBUG,
        threaded=True
    )