import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import httpx

from src.models.ollama_provider import OllamaProvider
from src.models.types import ModelRequest, Message
from src.config import settings

@pytest.mark.asyncio
async def test_load_model_respects_settings():
    provider = OllamaProvider(base_url="http://test-ollama:11434", default_model="test-model")
    
    # Temporarily override settings
    original_timeout = settings.ollama_load_timeout
    original_keep_alive = settings.ollama_keep_alive
    settings.ollama_load_timeout = 42.0
    settings.ollama_keep_alive = "20m"
    
    try:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "success"}
        
        # Patch httpx.AsyncClient
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            
            success = await provider.load_model("test-model")
            
            assert success is True
            # Check timeout was passed to AsyncClient
            mock_client_cls.assert_called_with(timeout=42.0)
            
            # Check that keep_alive was passed in the request body
            mock_client.post.assert_called_once()
            args, kwargs = mock_client.post.call_args
            assert kwargs["json"]["keep_alive"] == "20m"
            
    finally:
        settings.ollama_load_timeout = original_timeout
        settings.ollama_keep_alive = original_keep_alive

@pytest.mark.asyncio
async def test_generate_respects_settings():
    provider = OllamaProvider(base_url="http://test-ollama:11434", default_model="test-model")
    provider._loaded_model = "test-model" # Mark it loaded to avoid load_model call
    
    original_timeout = settings.ollama_client_timeout
    original_remote = settings.ollama_is_remote
    settings.ollama_client_timeout = 99.0
    settings.ollama_is_remote = False
    
    try:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": "Hello response"},
            "prompt_eval_count": 10,
            "eval_count": 20
        }
        
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            
            req = ModelRequest(messages=[Message(role="user", content="hello")])
            resp = await provider.generate(req)
            
            assert resp.content == "Hello response"
            mock_client_cls.assert_called_with(timeout=99.0)
    finally:
        settings.ollama_client_timeout = original_timeout
        settings.ollama_is_remote = original_remote

@pytest.mark.asyncio
async def test_remote_profile_settings():
    provider = OllamaProvider(base_url="http://test-ollama:11434", default_model="test-model")
    provider._loaded_model = "test-model"
    
    original_timeout = settings.ollama_client_timeout
    original_remote = settings.ollama_is_remote
    settings.ollama_client_timeout = 150.0
    settings.ollama_is_remote = True
    
    try:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": "Hello remote response"},
            "prompt_eval_count": 5,
            "eval_count": 5
        }
        
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            
            req = ModelRequest(messages=[Message(role="user", content="hello")])
            await provider.generate(req)
            
            # For remote cloud profiles, we want to configure custom connection pool limits
            # like httpx.Limits and custom connection timeout settings. Let's make sure
            # httpx.AsyncClient was instantiated with limits and a custom timeout object.
            mock_client_cls.assert_called_once()
            args, kwargs = mock_client_cls.call_args
            
            assert "limits" in kwargs
            assert isinstance(kwargs["limits"], httpx.Limits)
            assert kwargs["limits"].max_connections == 10
            assert kwargs["limits"].max_keepalive_connections == 5
            
            # The timeout can be either a float or an httpx.Timeout object.
            # Let's ensure a customized httpx.Timeout was used to set a higher connect/pool timeout
            # (e.g. timeout=httpx.Timeout(150.0, connect=10.0))
            assert "timeout" in kwargs
            assert isinstance(kwargs["timeout"], httpx.Timeout)
            assert kwargs["timeout"].connect == 10.0
            assert kwargs["timeout"].read == 150.0
            
    finally:
        settings.ollama_client_timeout = original_timeout
        settings.ollama_is_remote = original_remote
