from ollama import Client
import os

print('Iniciando client ollama...')
client_ollama = Client(host=os.getenv('OLLAMA_SERVICE_URL'))
print('Concluído')

def chama(contexto:str, query:str) -> dict:
    return client_ollama.chat(
        model='chatknd',
        messages=[
            { 'role': 'system', 'content': f'\n{contexto}\n\nPergunta do usuário:\n{query}' },
            { 'role': 'user', 'content': query }
        ]
    )