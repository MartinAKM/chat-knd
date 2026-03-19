from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from ollama import Client
from model.request import Request
from services.request import chama_api
import os

load_dotenv()

app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

print('Iniciando client ollama...')
client_ollama = Client(host=os.getenv('OLLAMA_SERVICE_URL'))
print('Concluído')

@app.get('/')
def test():
    return { 'text': 'API up and running!' }

@app.post('/response')
def gera_resposta(request:Request):
    print('Recuperando contexto...')
    contexto = chama_api('QDRANT', { 'query': request.query })
    print('Concluído')

    resposta_ollama = None

    try:
        print('Resposta ollama...')
        resposta_ollama = client_ollama.chat(
            model='chatknd',
            messages=[
                { 'role': 'system', 'content': f'\n{contexto}\n\nPergunta do usuário:\n{request.query}' },
                { 'role': 'user', 'content': request.query }
            ]
        )
        print('Concluído')
    except Exception as e:
        print('Erro Ollama:', str(e))

    if resposta_ollama:
        return resposta_ollama
    else:
        return { 'text': 'nothing' }