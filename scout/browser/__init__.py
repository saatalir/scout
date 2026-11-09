"""Browser layer: named profiles on local or remote hosts, driven over CDP."""

from .config import BrowserConfig, Host, Profile, SearchConfig, load_browser_config
from .hosts import BrowserHostError, Endpoint
from .session import BrowserSession, open_browser

__all__ = ["BrowserConfig", "BrowserHostError", "BrowserSession", "Endpoint", "Host", "Profile",
           "SearchConfig", "load_browser_config", "open_browser"]
