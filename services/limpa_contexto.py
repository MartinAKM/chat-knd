def retorna_contexto_limpo(contextos:list[dict]) -> str:
    contexto_limpo = ''

    for contexto in contextos:
        payload = contexto['payload']
        metadata = payload['metadata']

        contexto_limpo += '[ Atendimento: ' + str(payload['atendimento_id']) + ' Data Cadastro: ' + metadata['data_criacao_atendimento'] + ' Cliente: ' + metadata['origem_msg'] + ' ]'

        contexto_limpo += ' [ Data Mensagem: ' +  metadata['data_hora_msg'] + ' Usuário Mensagem: ' + metadata['usuario_nome'] + ' ]'

        contexto_limpo += ' Texto da Mensagem: ' + payload['text']

        contexto_limpo += '\n'
    
    return contexto_limpo