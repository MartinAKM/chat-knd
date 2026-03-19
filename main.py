from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from model.request import Request
from services.request import chama_api
from settings.llm import prompt_llm
from openai import OpenAI
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
    contexto = chama_api('QDRANT', { 'query': request.query })
    print('Concluído')

    resposta_lmstudio = None

    print('Resposta LMStudio...')
    try:
        client = OpenAI(base_url=os.getenv('LMSTUDIO_URL'), api_key="lm-studio")
        resposta_lmstudio = client.chat.completions.create(
            model=os.getenv('LMSTUDIO_MODEL'),
            messages=[
                { 'role': 'system', 'content': prompt_llm + f'\n{contexto}\n\nPergunta do usuário:\n{request.query}' },
                { 'role': 'user', 'content': request.query }
            ],
            extra_body={ 'include_reasoning': False },
            temperature=0.3,
            stream=False
        )
    except Exception as e:
        print('Erro LMStudio:', str(e))
    print('Concluído')
    
    return resposta_lmstudio