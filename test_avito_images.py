"""Gallery extraction and MCP image delivery; no live shop requests."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

import avito
from mcp.server.fastmcp import Image


class ImagesTest(unittest.TestCase):
    def setUp(self):
        self.tab = Mock()
        self.tab.js.return_value = dict(blocked=False, title='Станок',
                                       url='https://www.avito.ru/item_123',
                                       urls=['https://cdn/one', 'https://cdn/two'])
        p = patch.object(avito, 'tab_for', return_value=self.tab)
        p.start()
        self.addCleanup(p.stop)

    def test_delivers_image_blocks_in_order_and_honors_limit(self):
        photo = Image(data=b'jpeg', format='jpeg')
        with patch('pics._get', return_value=photo) as download:
            out = avito.images('/item_123', 1)
        self.assertEqual(out[1:], ['#1', photo])
        self.assertIn('фотографий: 2; показываю 1', out[0])
        download.assert_called_once_with('https://cdn/one')
        self.tab.close.assert_called_once()
        self.assertEqual(self.tab.goto.call_args.args[0], 'https://www.avito.ru/item_123')

    def test_failed_download_keeps_number_and_next_photo(self):
        photo = Image(data=b'jpeg', format='jpeg')
        with patch('pics._get', side_effect=[ValueError('HTTP 403'), photo]):
            out = avito.images('/item_123')
        self.assertIn('#1: не скачалась', out[1])
        self.assertEqual(out[2:], ['#2', photo])

    def test_blocked_and_empty_pages_are_explained(self):
        for update, text in [({'blocked': True}, 'капчу'), ({'urls': []}, 'не найдены')]:
            with self.subTest(update=update), patch.object(avito.pics, 'fetch') as fetch:
                self.tab.js.return_value.update(blocked=False, urls=[])
                self.tab.js.return_value.update(update)
                self.assertIn(text, avito.images('/item_123')[0])
                fetch.assert_not_called()

    def test_releases_tab_on_extraction_error(self):
        self.tab.js.side_effect = RuntimeError('navigation failed')
        with self.assertRaises(RuntimeError):
            avito.images('/item_123')
        self.tab.close.assert_called_once()

    def test_invalid_arguments_do_not_open_browser(self):
        with patch.object(avito, 'tab_for') as tab:
            for url, limit in [('/item_123', 0), ('/item_123', 21),
                               ('https://example.org/', 1), ('//evil.org/', 1)]:
                with self.assertRaises(ValueError):
                    avito.images(url, limit)
            tab.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'node needed for extraction fixture')
    def test_extracts_full_gallery_without_lazy_images_or_recommendations(self):
        fixture = '''
        global.window = {__staticRouterHydrationData: {loaderData: {route: {buyerItem: {item: {
          title: 'Gallery', imageUrls: [
            {'150x110': 'thumb', '1280x960': 'first'},
            {'640x480': 'second'}, {'1280x960': 'first'}]
        }}, recommendations: {imageUrls: [{'1280x960': 'wrong'}]}}}}};
        global.document = {title: '', body: {innerText: ''}, querySelector: () => null,
          querySelectorAll: () => {throw Error('must use full state gallery')}};
        global.location = {href: 'https://www.avito.ru/item_123?context=x'};
        '''
        got = subprocess.check_output(['node', '-e', fixture +
                                      'console.log(JSON.stringify(' + avito.IMAGES_JS + '))'], text=True)
        self.assertEqual(json.loads(got)['urls'], ['first', 'second'])


if __name__ == '__main__':
    unittest.main()
