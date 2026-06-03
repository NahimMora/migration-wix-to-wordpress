"""Conservative cleanup and inspection of Wix HTML content."""

from __future__ import annotations

from urllib.parse import urlparse

from bs4 import BeautifulSoup


ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "em",
    "h2",
    "h3",
    "h4",
    "li",
    "ol",
    "p",
    "strong",
    "ul",
    "b",
    "i",
    "img",
}
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title", "target", "rel"},
    "img": {"src", "alt", "title"},
}
REMOVE_TAGS = {"script", "style", "noscript", "object", "embed"}


def clean_html(raw_html: str, allow_iframes: bool = False) -> str:
    """Remove unsafe/noisy markup while preserving normal article content."""

    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup.find_all(REMOVE_TAGS):
        tag.decompose()

    for iframe in soup.find_all("iframe"):
        if allow_iframes:
            iframe.attrs = {key: value for key, value in iframe.attrs.items() if key in {"src", "title"}}
        else:
            iframe.decompose()

    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        allowed = ALLOWED_ATTRIBUTES.get(tag.name, set())
        tag.attrs = {key: value for key, value in tag.attrs.items() if key in allowed}
        if tag.name == "a" and tag.get("target") == "_blank":
            tag["rel"] = "noopener noreferrer"

    cleaned = str(soup).strip()
    return cleaned


def visible_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True)


def extract_internal_links(html: str, production_domain: str) -> list[str]:
    if not html:
        return []
    domain = urlparse(production_domain).netloc.lower()
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if not href:
            continue
        parsed = urlparse(href)
        if not parsed.netloc or parsed.netloc.lower() == domain:
            links.append(href)
    return links
