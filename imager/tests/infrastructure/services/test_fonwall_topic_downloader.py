import unittest

from src.infrastructure.services.topic_downloader.fonwall_topic_downloader import \
    FonwallTopicDownloader


class TestFonwallTopicDownloader(unittest.TestCase):
    def test_extract_links_from_html_deduplicates_results(self):
        html = '''
        <html>
            <img class="photo-item__img" data-big-src="https://example.com/a.jpg" />
            <img class="photo-item__img" data-big-src="https://example.com/a.jpg" />
            <img class="photo-item__img" data-big-src="https://example.com/b.jpg" />
            <img class="other-class" data-big-src="https://example.com/c.jpg" />
        </html>
        '''

        links = FonwallTopicDownloader.extract_links_from_html(html)

        self.assertEqual(
            links,
            ['https://example.com/a.jpg', 'https://example.com/b.jpg']
        )

    def test_sanitize_group_name_preserves_unicode_and_removes_invalid_chars(self):
        group_name = ' коты / мемы : 2026 '

        sanitized = FonwallTopicDownloader.sanitize_group_name(group_name)

        self.assertEqual(sanitized, 'коты_мемы_2026')
