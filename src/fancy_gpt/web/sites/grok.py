from .base import SiteContract

GROK_SITE = SiteContract(
    id="grok", hosts=("grok.com", "x.com"),
    required_browser_features=("dom", "javascript", "persistent-auth"),
    metadata={"fresh_url": "https://grok.com/"},
)
