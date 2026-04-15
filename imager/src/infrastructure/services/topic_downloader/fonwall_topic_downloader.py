import hashlib
import os
import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import quote_plus, unquote, urlsplit
from urllib.request import Request, urlopen

from settings import settings
from src.shared_kernel.result import Result


class FonwallImageHtmlParser(HTMLParser):
    TARGET_CLASS = 'photo-item__img'
    TARGET_ATTR = 'data-big-src'

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != 'img':
            return

        attrs_map = dict(attrs)
        class_names = (attrs_map.get('class') or '').split()
        image_url = attrs_map.get(self.TARGET_ATTR)
        if self.TARGET_CLASS in class_names and image_url:
            self.links.append(image_url)


@dataclass
class TopicDownloadStats:
    keyword: str
    group_name: str
    downloaded_count: int
    skipped_existing_count: int
    discovered_count: int
    directory: str

    def to_dict(self) -> dict:
        return asdict(self)


class FonwallTopicDownloader:
    BASE_URL = 'https://fonwall.ru/search?q={keyword}&page={page}'
    USER_AGENT = 'IMagerBOT/1.0'

    def __init__(self, base_directory: str | None = None, timeout_seconds: int = 15):
        self.base_directory = base_directory or settings.image_groups_relative_path
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def sanitize_group_name(group_name: str) -> str:
        sanitized = re.sub(r'[\\/:*?"<>|]+', '_', group_name).strip()
        sanitized = re.sub(r'\s+', '_', sanitized)
        sanitized = re.sub(r'_+', '_', sanitized)
        sanitized = sanitized.strip('._')
        if not sanitized:
            raise ValueError('Group name cannot be empty')
        return sanitized

    @staticmethod
    def extract_links_from_html(html: str) -> list[str]:
        parser = FonwallImageHtmlParser()
        parser.feed(html)

        unique_links: list[str] = []
        seen: set[str] = set()
        for link in parser.links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)
        return unique_links

    def download_topic(
        self,
        keyword: str,
        group_name: str | None = None,
        limit: int | None = None,
        max_pages: int = 25,
    ) -> Result:
        try:
            normalized_keyword = keyword.strip()
            if not normalized_keyword:
                return Result.Error('Keyword cannot be empty')

            normalized_group = self.sanitize_group_name(group_name or normalized_keyword)
            target_directory = os.path.join(self.base_directory, normalized_group)
            os.makedirs(target_directory, exist_ok=True)

            image_links = self._collect_image_links(normalized_keyword, max_pages=max_pages)
            if limit is not None:
                if limit <= 0:
                    return Result.Error('Limit must be greater than 0')
                image_links = image_links[:limit]

            downloaded_count = 0
            skipped_existing_count = 0
            for image_link in image_links:
                file_name = self._build_file_name(image_link)
                file_path = os.path.join(target_directory, file_name)
                if os.path.exists(file_path):
                    skipped_existing_count += 1
                    continue

                image_content_result = self._download_binary(image_link)
                if not image_content_result.is_success:
                    continue

                with open(file_path, 'wb') as image_file:
                    image_file.write(image_content_result.value)
                downloaded_count += 1

            stats = TopicDownloadStats(
                keyword=normalized_keyword,
                group_name=normalized_group,
                downloaded_count=downloaded_count,
                skipped_existing_count=skipped_existing_count,
                discovered_count=len(image_links),
                directory=target_directory,
            )
            return Result.Success(stats)
        except Exception as e:
            return Result.Error(f'Failed to download topic: {e}')

    def _collect_image_links(self, keyword: str, max_pages: int) -> list[str]:
        collected_links: list[str] = []
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            page_html_result = self._fetch_html(keyword, page)
            if not page_html_result.is_success:
                break

            page_links = self.extract_links_from_html(page_html_result.value)
            fresh_links = [link for link in page_links if link not in seen]
            if not fresh_links:
                break

            seen.update(fresh_links)
            collected_links.extend(fresh_links)

        return collected_links

    def _fetch_html(self, keyword: str, page: int) -> Result:
        search_url = self.BASE_URL.format(keyword=quote_plus(keyword), page=page)
        return self._request_text(search_url)

    def _request_text(self, url: str) -> Result:
        try:
            request = Request(url, headers={'User-Agent': self.USER_AGENT})
            with urlopen(request, timeout=self.timeout_seconds) as response:
                encoding = response.headers.get_content_charset() or 'utf-8'
                return Result.Success(response.read().decode(encoding, errors='ignore'))
        except Exception as e:
            return Result.Error(f'Failed to fetch url {url}: {e}')

    def _download_binary(self, url: str) -> Result:
        try:
            request = Request(url, headers={'User-Agent': self.USER_AGENT})
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return Result.Success(response.read())
        except Exception as e:
            return Result.Error(f'Failed to download image {url}: {e}')

    def _build_file_name(self, image_url: str) -> str:
        parsed_url = urlsplit(image_url)
        raw_name = os.path.basename(unquote(parsed_url.path))
        safe_name = self._sanitize_file_name(raw_name)
        if safe_name:
            return safe_name

        extension = os.path.splitext(parsed_url.path)[1] or '.jpg'
        digest = hashlib.sha256(image_url.encode('utf-8')).hexdigest()[:16]
        return f'image_{digest}{extension}'

    @staticmethod
    def _sanitize_file_name(file_name: str) -> str:
        if not file_name:
            return ''
        safe_name = re.sub(r'[\\/:*?"<>|]+', '_', file_name).strip()
        return safe_name[:255]
