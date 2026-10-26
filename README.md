# OctoPrint Marketplace Link

Plugin para **OctoPrint** que integra impressoras 3D a sistemas externos por meio de **webhooks HTTP**. Envia automaticamente eventos e informações de impressão em JSON para uma URL configurada pelo usuário.

## Funcionalidades

- Notificações de conexão, início, pausa, retomada, conclusão, cancelamento e falhas de impressão.
- Dados da impressora, arquivo e progresso da impressão.
- Suporte a impressoras conectadas ao OctoPrint e integração opcional com **Bambu Lab via MQTT local**.
- Heartbeat periódico para monitoramento de disponibilidade.
- Autenticação opcional com Bearer Token, botão de teste e acompanhamento do status dos envios.

## Como usar

1. Instale o plugin no mesmo ambiente Python do OctoPrint, a partir da pasta `plugin/`:
   ```bash
   pip install ./plugin
   ```
2. Reinicie o OctoPrint e abra **Settings → Marketplace Link**.
3. Informe a URL do webhook, configure a marca/modelo e, se necessário, um token de autenticação.
4. Clique em **Testar webhook** para validar a comunicação.
5. Para impressoras Bambu Lab, habilite o modo Bambu e configure IP, número de série e código de acesso da impressora.

> **Observação:** a integração Bambu depende do acesso MQTT na rede local; algumas versões de firmware exigem Developer Mode. O plugin envia dados ao endpoint escolhido, mas não inclui o servidor ou site que os recebe.

## Estrutura

- `plugin/` — código-fonte e documentação detalhada do plugin.
- `test_e2e/` — testes de integração e receptor de webhook para desenvolvimento.
- `preview/` — prévia da interface.

Desenvolvido por **Matheus Portilho**.

Mais detalhes técnicos e instruções estão em [plugin/README.md](plugin/README.md).
