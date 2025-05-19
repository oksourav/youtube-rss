#!/usr/bin/env python3
"""
Test Suite for YouTube RSS Shorts Filter
"""

import unittest
import json
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import sys
import os

# Add the parent directory to the path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, processor, stats, Config

class TestYouTubeRSSProcessor(unittest.TestCase):
    """Test cases for the YouTubeRSSProcessor class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.processor = processor
        self.app = app.test_client()
        self.app.testing = True
    
    def test_channel_feed_url_generation(self):
        """Test RSS feed URL generation for channels."""
        # Test channel ID
        channel_url = self.processor.get_channel_feed_url("UCxxxxxx")
        self.assertEqual(channel_url, "https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxxxx")
        
        # Test username
        username_url = self.processor.get_channel_feed_url("testuser", is_username=True)
        self.assertEqual(username_url, "https://www.youtube.com/feeds/videos.xml?user=testuser")
    
    def test_duration_extraction(self):
        """Test duration extraction from video titles."""
        # Test valid duration formats
        self.assertEqual(self.processor.extract_duration_from_title("[2:30] Test Video"), 150)
        self.assertEqual(self.processor.extract_duration_from_title("[0:45] Quick Video"), 45)
        self.assertEqual(self.processor.extract_duration_from_title("[10:05] Long Video"), 605)
        
        # Test invalid formats
        self.assertIsNone(self.processor.extract_duration_from_title("Test Video"))
        self.assertIsNone(self.processor.extract_duration_from_title("2:30 Test Video"))
        self.assertIsNone(self.processor.extract_duration_from_title("[invalid] Test Video"))
    
    def test_shorts_detection(self):
        """Test YouTube Shorts detection logic."""
        # Test keyword detection
        short_entry = {
            'title': 'Amazing #shorts compilation',
            'summary': 'Check out these shorts'
        }
        is_short, reason = self.processor.is_short_video(short_entry)
        self.assertTrue(is_short)
        self.assertIn("keywords", reason)
        
        # Test duration detection
        duration_entry = {
            'title': '[0:30] Quick tutorial',
            'summary': 'Learn something fast'
        }
        is_short, reason = self.processor.is_short_video(duration_entry)
        self.assertTrue(is_short)
        self.assertIn("Duration from title", reason)
        
        # Test normal video
        normal_entry = {
            'title': '[5:30] Detailed explanation',
            'summary': 'In-depth tutorial'
        }
        is_short, reason = self.processor.is_short_video(normal_entry)
        self.assertFalse(is_short)
    
    def test_title_duration_addition(self):
        """Test adding duration prefix to titles."""
        # Test with duration
        title_with_duration = self.processor.add_duration_to_title("Test Video", 150)
        self.assertEqual(title_with_duration, "[2m30s] Test Video")
        
        # Test with seconds only
        title_seconds = self.processor.add_duration_to_title("Test Video", 45)
        self.assertEqual(title_seconds, "[45s] Test Video")
        
        # Test without duration
        title_no_duration = self.processor.add_duration_to_title("Test Video")
        self.assertEqual(title_no_duration, "Test Video")
        
        # Test with existing duration in title
        existing_duration = self.processor.add_duration_to_title("[3:00] Test Video", 180)
        self.assertEqual(existing_duration, "[3:00] Test Video")

class TestFlaskRoutes(unittest.TestCase):
    """Test cases for Flask routes."""
    
    def setUp(self):
        """Set up test client."""
        self.app = app.test_client()
        self.app.testing = True
    
    def test_dashboard_route(self):
        """Test the main dashboard route."""
        response = self.app.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'YouTube RSS Shorts Filter', response.data)
    
    def test_rss_discovery_route(self):
        """Test the RSS discovery route."""
        response = self.app.get('/rss-discovery')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'rel="alternate"', response.data)
        self.assertIn(b'application/rss+xml', response.data)
    
    def test_health_check_route(self):
        """Test the health check endpoint."""
        response = self.app.get('/health')
        self.assertEqual(response.status_code, 200)
        
        data = json.loads(response.data)
        self.assertEqual(data['status'], 'healthy')
        self.assertIn('timestamp', data)
        self.assertIn('configuration', data)
        self.assertIn('statistics', data)
    
    def test_stats_route(self):
        """Test the statistics endpoint."""
        response = self.app.get('/stats')
        self.assertEqual(response.status_code, 200)
        
        data = json.loads(response.data)
        self.assertIn('requests', data)
        self.assertIn('videos_processed', data)
        self.assertIn('shorts_filtered', data)
        self.assertIn('configuration', data)
    
    def test_debug_route_disabled(self):
        """Test debug route when debug mode is disabled."""
        # Ensure debug is disabled
        Config.DEBUG = False
        response = self.app.get('/debug')
        self.assertEqual(response.status_code, 404)
    
    @patch('app.Config.DEBUG', True)
    def test_debug_route_enabled(self):
        """Test debug route when debug mode is enabled."""
        response = self.app.get('/debug')
        self.assertEqual(response.status_code, 200)
        
        data = json.loads(response.data)
        self.assertIn('environment_variables', data)
        self.assertIn('feed_urls', data)
        self.assertIn('statistics', data)
    
    @patch('app.processor.process_feeds')
    def test_rss_feed_route(self, mock_process_feeds):
        """Test RSS feed generation route."""
        # Mock the processor response
        mock_entries = [
            {
                'title': 'Test Video 1',
                'link': 'https://youtube.com/watch?v=test1',
                'published': '2024-01-01T12:00:00Z',
                'summary': 'Test summary 1',
                'author': 'Test Author',
                'id': 'test1'
            }
        ]
        mock_process_feeds.return_value = mock_entries
        
        response = self.app.get('/rss')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'application/atom+xml; charset=utf-8')
        self.assertIn(b'<?xml version="1.0"', response.data)
        self.assertIn(b'<feed xmlns="http://www.w3.org/2005/Atom"', response.data)
    
    def test_not_found_route(self):
        """Test 404 handler."""
        response = self.app.get('/nonexistent')
        self.assertEqual(response.status_code, 404)
        
        data = json.loads(response.data)
        self.assertEqual(data['error'], 'Not Found')
        self.assertIn('available_endpoints', data)

class TestConfiguration(unittest.TestCase):
    """Test cases for configuration handling."""
    
    def test_config_defaults(self):
        """Test default configuration values."""
        self.assertEqual(Config.MAX_SHORT_DURATION, 90)
        self.assertEqual(Config.INCLUDE_DURATION, True)
        self.assertEqual(Config.STRICT_FILTER, False)
        self.assertEqual(Config.DEBUG, False)
        self.assertEqual(Config.PORT, 5000)
    
    @patch.dict(os.environ, {
        'MAX_SHORT_DURATION': '60',
        'INCLUDE_DURATION': 'false',
        'STRICT_FILTER': 'true',
        'DEBUG': 'true'
    })
    def test_config_from_environment(self):
        """Test configuration loading from environment variables."""
        # Reload config to pick up environment changes
        from importlib import reload
        import app
        reload(app)
        
        self.assertEqual(app.Config.MAX_SHORT_DURATION, 60)
        self.assertEqual(app.Config.INCLUDE_DURATION, False)
        self.assertEqual(app.Config.STRICT_FILTER, True)
        self.assertEqual(app.Config.DEBUG, True)

class TestStatistics(unittest.TestCase):
    """Test cases for statistics tracking."""
    
    def setUp(self):
        """Reset statistics before each test."""
        stats.requests = 0
        stats.videos_processed = 0
        stats.shorts_filtered = 0
        stats.errors = 0
    
    def test_efficiency_calculation(self):
        """Test filter efficiency calculation."""
        # Test with no videos processed
        self.assertEqual(stats.efficiency(), 0)
        
        # Test with some filtering
        stats.videos_processed = 100
        stats.shorts_filtered = 25
        self.assertEqual(stats.efficiency(), 25.0)
        
        # Test with perfect filtering
        stats.videos_processed = 50
        stats.shorts_filtered = 50
        self.assertEqual(stats.efficiency(), 100.0)
    
    def test_uptime_tracking(self):
        """Test uptime calculation."""
        uptime = stats.uptime()
        self.assertIsInstance(uptime, float)
        self.assertGreaterEqual(uptime, 0)

if __name__ == '__main__':
    # Run the tests
    unittest.main(verbosity=2)