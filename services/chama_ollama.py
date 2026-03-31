from ollama import Client

def chama(client_ollama:Client, contexto:str, query:str) -> dict:
    return client_ollama.chat(
        model='chatknd',
        messages=[
            { 'role': 'system', 'content': f'\n{contexto}\n\nPergunta do usuário:\n{query}' },
            { 'role': 'user', 'content': query }
        ]
    )