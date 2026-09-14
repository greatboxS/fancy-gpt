from .base import SiteContract

GLM_SITE = SiteContract(
    id="glm", hosts=("chat.z.ai", "z.ai"),
    required_browser_features=("dom", "javascript", "persistent-auth"),
    metadata={"fresh_url": "https://chat.z.ai/"},
)
