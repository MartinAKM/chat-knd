from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from model.request import Request
from services.request import chama_api

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
    contexto = chama_api('QDRANT', { 'query': request.query })
    