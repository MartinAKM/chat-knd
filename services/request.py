import requests
import os

def chama_api(qual:str, tipo:str, dados:dict):
    url = os.getenv((qual.upper() + '_SERVICE_URL'))

    resposta = requests.post(url, json=dados)

    if resposta.status_code == 200:
        return resposta.json()
    else:
        print('Erro:', resposta.status_code, ', Detalhes:', resposta.json())
        raise