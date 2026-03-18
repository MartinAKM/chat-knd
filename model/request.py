from pydantic import BaseModel

class Request(BaseModel):
    query:str