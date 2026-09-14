from .base import SiteContract

KIMI_SITE = SiteContract(
    id="kimi", hosts=("www.kimi.com", "kimi.com", "www.kimi.ai", "kimi.ai"),
    required_browser_features=("dom", "javascript", "persistent-auth"),
    metadata={"fresh_url": "https://www.kimi.com/"},
)
