from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from ollama import Client
from model.request import Request
from services.request import chama_api
from services.limpa_contexto import retorna_contexto_limpo
from services.chama_ollama import chama
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
    contexto = retorna_contexto_limpo(chama_api('QDRANT', { 'query': request.query }))
    print('Concluído\n', contexto)

    contexto_mensagens = chama_api('BD', { 'query': request.query })
    
    if contexto_mensagens:
        contexto += '\nDetalhes das mensagens citadas:\n' + contexto_mensagens['text']
    
    print('\n\n\ncontexto:', contexto)

    print('Resposta ollama...')
    resposta_ollama = chama(client_ollama, contexto, request.query)
    print('Concluído')

    #return resposta_ollama['message']['content']
    return resposta_ollama
    return None