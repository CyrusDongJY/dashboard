"""Turn frozen, offline PNGs into email CID parts without remote image requests."""
from base64 import b64decode
from html.parser import HTMLParser


class ImageSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            source = dict(attrs).get("src", "")
            if source.startswith("data:image/png;base64,") and source not in self.sources:
                self.sources.append(source)


def add_html_with_images(message, html, identifier):
    parser = ImageSources()
    parser.feed(html)
    related = []
    for i, source in enumerate(parser.sources):
        payload = b64decode(source.split(",", 1)[1], validate=True)
        if not payload.startswith(b"\x89PNG\r\n\x1a\n") or len(payload) > 4_000_000:
            raise ValueError("Invalid inline PNG")
        cid = f"digest-chart-{identifier}-{i}@inline"
        html = html.replace(source, "cid:" + cid)
        related.append((cid, payload))
    message.add_alternative(html, subtype="html")
    part = message.get_payload()[-1]
    for cid, payload in related:
        part.add_related(payload, maintype="image", subtype="png", cid="<" + cid + ">", disposition="inline")
