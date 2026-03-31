def retorna_contexto_limpo(contextos:list[dict]) -> str:
    contexto_limpo = ''

    for i, contexto in enumerate(contextos):
        # payload = contexto['payload']
        # metadata = payload['metadata']

        # contexto_limpo += '[ Atendimento: ' + str(payload['atendimento_id']) + ' Data Cadastro: ' + metadata['data_criacao_atendimento'] + ' Cliente: ' + metadata['origem_msg'] + ' ]'

        # contexto_limpo += ' [ Data Mensagem: ' +  metadata['data_hora_msg'] + ' Usuário Mensagem: ' + metadata['usuario_nome'] + ' ]'

        # contexto_limpo += ' Texto da Mensagem: ' + payload['text']

        # contexto_limpo += '\n'
        contexto_limpo += f'Atendimento: {str(contexto['payload']['atendimento_id'])}, Mensagem {(i + 1)}: {contexto['payload']['text']}\n'
    
    return contexto_limpo