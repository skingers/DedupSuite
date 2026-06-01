import json
import urllib.error
from unittest.mock import MagicMock, patch
import pytest

from chat_engine import generate_rag_response

def test_generate_rag_response_success():
    # Mock Vector Engine
    mock_vector = MagicMock()
    mock_vector.query_vault.return_value = {
        "documents": ["Chroma document context 1", "Chroma document context 2"],
        "metadatas": [{"source": "test"}, {"source": "test"}]
    }

    # Mock HTTP Response
    mock_response_data = [
        b'{"response": "Hello "}',
        b'{"response": "world!"}'
    ]
    
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response_data
    
    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        generator = generate_rag_response("hello", mock_vector)
        responses = list(generator)
        
        # Verify result
        assert "".join(responses) == "Hello world!"
        # Verify query_vault was called
        mock_vector.query_vault.assert_called_once_with("hello")
        # Verify request parameters
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/json"
        
        # Verify data payload contains query prompt
        data = json.loads(req.data.decode("utf-8"))
        assert data["model"] == "gemma4:e2b"
        assert "Chroma document context 1" in data["prompt"]

def test_generate_rag_response_no_context():
    mock_vector = MagicMock()
    mock_vector.query_vault.return_value = {
        "documents": [],
        "metadatas": []
    }
    
    generator = generate_rag_response("hello", mock_vector)
    responses = list(generator)
    assert responses == ["I cannot find any relevant information in your vault."]

def test_generate_rag_response_vector_error():
    mock_vector = MagicMock()
    mock_vector.query_vault.side_effect = Exception("Chroma read failure")
    
    generator = generate_rag_response("hello", mock_vector)
    responses = list(generator)
    assert responses == ["[Vector Retrieval Error: Chroma read failure]\n"]

def test_generate_rag_response_connection_error():
    mock_vector = MagicMock()
    mock_vector.query_vault.return_value = {
        "documents": ["Context"],
        "metadatas": [{}]
    }
    
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        generator = generate_rag_response("hello", mock_vector)
        responses = list(generator)
        assert responses == ["\n[CONNECTION ERROR: Is the Ollama engine running?]"]
