from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from fancy_gpt.file_lock import exclusive_file_lock


class PageRuntime(Protocol):
    """Site-neutral browser page lifecycle."""

    name: str

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def new_page(self) -> Any: ...


class PlaywrightPageRuntime:
    name = "playwright-page-runtime"

    def __init__(self, profile_dir: Path | str, *, browser_type: str = "chromium", headless: bool = False) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        if browser_type not in {"chromium", "firefox"}:
            raise ValueError("browser_type must be chromium or firefox")
        self.browser_type = browser_type
        self.headless = headless
        self._playwright: Any = None
        self._context: Any = None
        self._profile_lock_cm: Any = None

    @staticmethod
    def _import_playwright() -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional runtime
            raise RuntimeError(
                "Playwright dependency is missing. Install the Playwright fallback before selecting this tunnel."
            ) from exc
        return sync_playwright

    def start(self) -> None:
        if self._context is not None:
            return
        sync_playwright = self._import_playwright()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_lock_cm = exclusive_file_lock(self.profile_dir / ".fancy-gpt-profile.lock")
        self._profile_lock_cm.__enter__()
        try:
            self._playwright = sync_playwright().start()
            launcher = getattr(self._playwright, self.browser_type)
            self._context = launcher.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
            )
        except Exception:
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None
            self._profile_lock_cm.__exit__(*__import__("sys").exc_info())
            self._profile_lock_cm = None
            raise

    def stop(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
                self._context = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None
        finally:
            if self._profile_lock_cm is not None:
                self._profile_lock_cm.__exit__(None, None, None)
                self._profile_lock_cm = None

    def new_page(self) -> Any:
        if self._context is None:
            raise RuntimeError("page runtime is not started")
        return self._context.new_page()


class CDPPageRuntime:
    name = "cdp-page-runtime"

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    def start(self) -> None:
        if self._context is not None:
            return
        sync_playwright = PlaywrightPageRuntime._import_playwright()
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(self.endpoint)
            contexts = self._browser.contexts
            if not contexts:
                raise RuntimeError("CDP browser has no context")
            self._context = contexts[0]
        except Exception:
            if self._playwright is not None:
                self._playwright.stop()
            self._playwright = None
            self._browser = None
            self._context = None
            raise

    def stop(self) -> None:
        # Never close the user's browser/context; only detach Playwright.
        self._context = None
        self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def new_page(self) -> Any:
        if self._context is None:
            raise RuntimeError("CDP runtime is not started")
        return self._context.new_page()
