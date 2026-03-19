from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

@app.get('/')
def test():
    return { 'text': 'API up and running!' }

@app.post('/response')
def gera_resposta(request:Request):
    print('Recuperando contexto...')
    contexto = retorna_contexto_limpo(chama_api('QDRANT', { 'query': request.query }))
    print('Concluído')

    print('Resposta ollama...')
    resposta_ollama = chama(contexto, request.query)
    print('Concluído')

    return resposta_ollama