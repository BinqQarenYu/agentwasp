import pytest
import time
import asyncio
from unittest.mock import MagicMock, patch
from src.skills.builtin.browser_screenshot_full_page import _do_screenshot_full_page

@pytest.fixture
def mock_driver():
    driver = MagicMock()
    driver.current_url = "http://example.com"
    driver.title = "Example Domain"
    driver.execute_script.return_value = 1000 # vh

    # Mock CDP cmd to simulate successful screenshot
    driver.execute_cdp_cmd.return_value = {"data": "R0lGODlhAQABAIAAAAUEBAAAACwAAAAAAQABAAACAkQBADs="} # tiny valid base64 image
    return driver

@patch("src.skills.builtin.browser_screenshot_full_page._capture_frame")
@patch("src.skills.builtin.browser_screenshot_full_page._get_driver")
@patch("src.skills.builtin.browser_screenshot_full_page._wait_for_page")
@patch("src.skills.builtin.browser_screenshot_full_page._dismiss_overlays")
@pytest.mark.asyncio
async def test_full_page_screenshot_performance(mock_dismiss, mock_wait, mock_get_driver, mock_capture, mock_driver):
    mock_get_driver.return_value = mock_driver
    mock_dismiss.return_value = False # First pass false
    mock_capture.return_value = ("test.png", b"test") # Mock capture frame

    start_time = time.time()

    # Run the blocking function
    result = await _do_screenshot_full_page(
        url="http://example.com",
        session="test",
        wait_ms=0, # Minimal wait per scroll
        scroll_step=500,
        chat_id="test_chat",
        user_id="test_user"
    )

    end_time = time.time()
    elapsed = end_time - start_time

    print(f"Elapsed time: {elapsed:.2f} seconds")

    assert elapsed >= 1.4

    mock_dismiss.side_effect = [True, False]

    start_time = time.time()
    result = await _do_screenshot_full_page(
        url="http://example.com",
        session="test",
        wait_ms=0,
        scroll_step=500,
        chat_id="test_chat",
        user_id="test_user"
    )
    end_time = time.time()
    elapsed2 = end_time - start_time

    print(f"Elapsed time (with accepted overlay): {elapsed2:.2f} seconds")

if __name__ == "__main__":
    pytest.main(["-v", "-s", __file__])
