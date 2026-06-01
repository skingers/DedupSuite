import json
import urllib.request
import urllib.error

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gemma4:e2b"

def generate_rag_response(query, vector_engine):
    # 1. Retrieve Context from ChromaDB
    context_text = ""
    try:
        results = vector_engine.query_vault(query)
        if results and results.get('documents') and len(results['documents']) > 0:
            context_text = "\n\n---\n\n".join(results['documents'])
    except Exception as e:
        yield f"[Vector Retrieval Error: {str(e)}]\n"
        return

    if not context_text:
        yield "I cannot find any relevant information in your vault."
        return

    # 2. Construct the Sovereign Prompt
    prompt = f"""You are a Sovereign AI assistant. Answer the user's question using ONLY the provided context. If the answer is not in the context, say 'I cannot find the answer in the vault.'

CONTEXT:
{context_text}

USER QUESTION:
{query}
"""

    # 3. Stream the generation via REST to keep memory footprint near zero
    data = {"model": MODEL_NAME, "prompt": prompt, "stream": True}
    req = urllib.request.Request(OLLAMA_URL, data=json.dumps(data).encode('utf-8'), method='POST')
    req.add_header('Content-Type', 'application/json')
    
    try:
        with urllib.request.urlopen(req) as response:
            for line in response:
                if line:
                    chunk = json.loads(line.decode('utf-8'))
                    yield chunk.get("response", "")
    except urllib.error.URLError:
        yield "\n[CONNECTION ERROR: Is the Ollama engine running?]"
