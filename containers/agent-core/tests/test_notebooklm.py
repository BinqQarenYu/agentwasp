import pytest
import asyncio
from unittest.mock import patch, MagicMock

from src.notebooklm import NotebookLMConnector
from selenium.common.exceptions import TimeoutException

@pytest.fixture
def connector():
    return NotebookLMConnector(
        notebook_url="https://notebooklm.google.com/notebook/placeholder",
        user_data_dir="/tmp/dummy_profile"
    )

@pytest.mark.asyncio
async def test_get_content_success(connector):
    mock_driver = MagicMock()
    mock_body = MagicMock()
    mock_body.text = "Dummy NotebookLM Content"
    mock_driver.find_element.return_value = mock_body

    with patch('src.notebooklm.webdriver.Chrome') as mock_chrome, \
         patch('src.notebooklm.WebDriverWait') as mock_wait, \
         patch('time.sleep'): # Mock sleep to speed up test

        mock_chrome.return_value = mock_driver
        mock_wait_instance = MagicMock()
        mock_wait.return_value = mock_wait_instance

        content = await connector.get_content()

        assert content == "Dummy NotebookLM Content"
        mock_chrome.assert_called_once()
        mock_driver.get.assert_called_with("https://notebooklm.google.com/notebook/placeholder")
        mock_driver.quit.assert_called_once()

@pytest.mark.asyncio
async def test_get_content_timeout(connector):
    with patch('src.notebooklm.webdriver.Chrome') as mock_chrome, \
         patch('src.notebooklm.WebDriverWait') as mock_wait:

        mock_driver = MagicMock()
        mock_chrome.return_value = mock_driver
        mock_wait_instance = MagicMock()
        mock_wait_instance.until.side_effect = TimeoutException("Timed out")
        mock_wait.return_value = mock_wait_instance

        content = await connector.get_content()

        assert "Timeout" in content
        mock_driver.quit.assert_called_once()
