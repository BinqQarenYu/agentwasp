import asyncio
import os
import structlog
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException


logger = structlog.get_logger(__name__)


class NotebookLMConnector:
    """
    Connector for Google NotebookLM.
    Uses Selenium with a local Chrome profile to bypass login restrictions.
    """

    def __init__(self, notebook_url: str, user_data_dir: Optional[str] = None, profile_dir: str = "Default"):
        self.notebook_url = notebook_url
        self.user_data_dir = user_data_dir or os.environ.get("CHROME_USER_DATA_DIR", "/data/chrome_profile")
        self.profile_dir = profile_dir
        self.driver: Optional[webdriver.Chrome] = None

    def _init_driver(self) -> None:
        if self.driver:
            return

        options = Options()
        # options.add_argument("--headless=new") # NotebookLM often blocks headless, might need to run non-headless
        options.add_argument(f"--user-data-dir={self.user_data_dir}")
        options.add_argument(f"--profile-directory={self.profile_dir}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        try:
            self.driver = webdriver.Chrome(options=options)
            logger.info("Initialized NotebookLM WebDriver", user_data_dir=self.user_data_dir)
        except Exception as e:
            logger.error("Failed to initialize WebDriver", error=str(e))
            raise

    def _close_driver(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.warning("Error quitting WebDriver", error=str(e))
            finally:
                self.driver = None

    def _extract_content_sync(self) -> str:
        """Synchronous method to drive the browser and extract content."""
        try:
            self._init_driver()
            assert self.driver is not None

            logger.info("Navigating to NotebookLM URL", url=self.notebook_url)
            self.driver.get(self.notebook_url)

            # NotebookLM is highly dynamic. We wait for the body to be present and contain some text.
            # A more robust selector would be needed for a production version based on actual NotebookLM DOM.
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            # Wait a bit more for dynamic content to render (React/Flutter/WebComponents)
            import time
            time.sleep(5)

            # Extract inner text of the body. In a real scenario we'd target specific elements
            # like the source document viewer or the chat transcript.
            body_element = self.driver.find_element(By.TAG_NAME, "body")
            text = body_element.text

            logger.info("Successfully extracted text from NotebookLM", text_length=len(text))
            return text

        except TimeoutException:
            logger.error("Timeout waiting for NotebookLM to load")
            return "Error: Timeout waiting for page to load."
        except WebDriverException as e:
            logger.error("WebDriver error during extraction", error=str(e))
            return f"Error: WebDriver exception: {str(e)}"
        except Exception as e:
            logger.error("Unexpected error during extraction", error=str(e))
            return f"Error: {str(e)}"
        finally:
            self._close_driver()

    async def get_content(self) -> str:
        """
        Asynchronously extract content from the NotebookLM notebook.
        Wraps the synchronous Selenium calls in a thread pool to avoid blocking the event loop.
        """
        logger.info("Starting NotebookLM content extraction task")
        # Run the blocking Selenium operations in a separate thread
        content = await asyncio.to_thread(self._extract_content_sync)
        return content
