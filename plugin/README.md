# OctoPrint-MarketplaceLink

Plugin OctoPrint que envia o estado da impressora para um **webhook configurável** a cada evento relevante (conexão, início/pausa/fim de impressão, erro). Sem servidor central — você aponta o plugin para qualquer URL HTTP que aceite POST de JSON.

- Versão: **0.3.0**
- Identificador: `marketplace_link`
- Compatibilidade: Python 3.9–3.12 / OctoPrint 1.4+
- Licença: AGPLv3

---

## Instalação

### Opção 1 — Via Plugin Manager (a partir de URL do GitHub)

1. No OctoPrint, abra **Settings → Plugin Manager → Get More**.
2. Clique em **... from URL** e cole:
   ```
   https://github.com/matheusprtlh/Plugin-Octoprint/archive/v0.3.0.zip
   ```
3. Confirme a instalação e reinicie o OctoPrint quando ele pedir.

### Opção 2 — Via wheel local (instalação offline)

Recebeu o arquivo `octoprint_marketplacelink-0.3.0-py3-none-any.whl` (ou o `.tar.gz`)?

```bash
# Active o venv onde o OctoPrint está rodando
source ~/oprint/bin/activate                    # caminho típico no OctoPi

# Instale o pacote
pip install octoprint_marketplacelink-0.3.0-py3-none-any.whl

# Reinicie o OctoPrint
sudo systemctl restart octoprint
```

Em qualquer caso, após reiniciar você verá o item **Marketplace Link** em **Settings**.

---

## Configuração

| Campo | Descrição |
|---|---|
| **URL** | Endpoint que receberá os POSTs JSON. Ex.: `https://meusite.com/api/octoprint`. |
| **Token (Bearer)** | Opcional. Se preenchido, é enviado no header `Authorization: Bearer <token>`. Mantido em escopo admin do OctoPrint — não vaza para usuários comuns. |
| **Marca** | `creality`, `elegoo`, `prusa`, `bambu`, `other`. Apenas metadado, repassado no payload. |
| **Modelo** | Texto livre. Ex.: `Ender 3 V2`. |
| **Heartbeat (min)** | Intervalo entre postagens periódicas (`event: "heartbeat"`) para indicar que a impressora continua viva. `0` desativa. Padrão: 5 min. |

Após preencher, clique em **Testar webhook**. O plugin envia um POST com `event: "test"` para a URL configurada e mostra o status HTTP retornado. Esse teste **não** afeta a telemetria de "Última entrega" mostrada abaixo.

---

## O que o servidor recebe

Cada POST tem `Content-Type: application/json` e o body abaixo:

```json
{
  "schema_version": 1,
  "timestamp": "2026-04-29T22:02:08+00:00",
  "event": "PrintStarted",
  "status": "printing",
  "printer": {
    "name": "OctoPrint",
    "brand": "creality",
    "model": "Ender 3 V2",
    "firmware_version": "Marlin 2.1.x"
  },
  "job": {
    "file": "benchy.gcode",
    "progress": 0.0
  }
}
```

### Campos

- **`schema_version`** (int): versão do payload. Comece a sua implementação aceitando `1`. Se mudar no futuro, será incrementado **só em mudanças não-aditivas** (renomeação, remoção, mudança de tipo). Adições de campos opcionais não bumpam.
- **`timestamp`** (string ISO 8601, UTC): momento em que o evento aconteceu no OctoPrint.
- **`event`** (string): nome do evento. Lista completa abaixo.
- **`status`** (string): `idle` | `printing` | `error` | `offline`.
- **`printer.name`** (string): nome do OctoPrint, configurável em **Settings → Appearance**.
- **`printer.brand`** (string): marca selecionada na settings do plugin.
- **`printer.model`** (string \| null): modelo informado na settings (campo livre).
- **`printer.firmware_version`** (string \| null): detectado pelo OctoPrint quando a impressora se conecta. Pode ser `null` se a impressora ainda não enviou o banner ou o firmware não responde a `M115`.
- **`job`** (object \| null): `null` exceto quando `status == "printing"`. Contém:
  - `file` (string \| null): nome do arquivo G-code em impressão.
  - `progress` (float \| null): percentual de conclusão, `0.0` a `100.0`.

### Eventos suportados

| `event` | Quando dispara |
|---|---|
| `Connected` | OctoPrint conectou na impressora (porta serial aberta). |
| `Disconnected` | OctoPrint desconectou da impressora. |
| `PrintStarted` | Início de uma impressão. |
| `PrintDone` | Impressão terminou com sucesso. |
| `PrintFailed` | Impressão falhou (erro durante a execução). |
| `PrintCancelled` | Usuário cancelou a impressão. |
| `PrintPaused` | Impressão pausada. |
| `PrintResumed` | Impressão retomada após pausa. |
| `Error` | Erro genérico reportado pelo OctoPrint. |
| `heartbeat` | Periódico, conforme **Heartbeat (min)**. |
| `test` | Disparado pelo botão **Testar webhook** — não persiste telemetria. |

### Header de autenticação

Se você preencher o campo **Token**, todo POST sai com:

```
Authorization: Bearer <seu-token>
```

Valide esse token no servidor. Se vazio, o plugin **não envia** o header.

### Resposta esperada

O plugin considera entrega bem-sucedida qualquer status `2xx`. Status `3xx`, `4xx`, `5xx` ou erro de rede contam como falha. O plugin **não tem retry**: cada evento gera um único POST, fire-and-forget. Se o seu servidor estiver fora do ar, eventos são perdidos. Recomendo dimensionar o servidor para aceitar bursts (durante uma impressão, eventos `PrintPaused`/`PrintResumed` podem chegar em sequência).

---

## Indicador na navbar

O plugin coloca um ícone na barra superior do OctoPrint:

| Ícone | Estado |
|---|---|
| Cinza, link quebrado | URL não configurada. |
| Verde, nuvem | Última entrega `2xx` ou ainda sem envios. |
| Amarelo, warning | Última entrega falhou (4xx/5xx ou erro de rede). |

Hover mostra detalhe ("Webhook: última entrega há 8s (HTTP 200)"). Clique abre a settings dialog.

---

## Diagnóstico

### Logs

Em **Settings → Logging**, ative DEBUG para `octoprint.plugins.marketplace_link`. Os logs vão para `~/.octoprint/logs/octoprint.log`:

- `Marketplace Link starting (webhook mode)` — startup.
- `evento <X> entregue (HTTP 200)` — sucesso (DEBUG).
- `webhook respondeu HTTP 500 para evento <X>: <body>` — erro do servidor.
- `falha de rede ao postar <X>: <detalhe>` — timeout, DNS, recusa de conexão.
- `evento <X> descartado: webhook_url não configurada` — plugin foi instalado mas não foi configurado ainda.

### Telemetria persistente

A página de settings mostra:

- **Última entrega**: status HTTP do último POST (ou "Falhou (rede)" se foi erro de transporte).
- **Tempo relativo**: "há 8s", "há 3min", etc.
- **Último erro**: corpo da resposta truncado em 200 chars, ou a mensagem da exception se foi rede.

Esses valores ficam preservados em settings entre restarts do OctoPrint.

---

## Caveat: Bambu Lab

Impressoras **Bambu Lab** (firmware fechado) **não aparecem para o OctoPrint via porta serial**. Esse plugin só dispara eventos quando o OctoPrint enxerga a impressora. Para integrar uma Bambu, o usuário precisa:

1. Manter firmware antigo na Bambu (versões mais recentes do firmware Bambu restringem ainda mais o acesso de fora).
2. Usar uma ferramenta separada (não inclusa neste plugin) que traduza MQTT da Bambu em comandos OctoPrint.

Sem isso, este plugin sozinho **não dirige uma Bambu Lab**. Recomendamos comunicar isso claramente ao usuário antes que ele instale.

---

## Suporte ao auto-update

O plugin declara um hook `softwareupdate.check_config` apontando para `<GITHUB_USER>/<GITHUB_REPO>` no GitHub. Enquanto os campos `GITHUB_USER` e `GITHUB_REPO` em `octoprint_marketplace_link/__init__.py` estiverem como `"TODO-org"`, o hook retorna vazio (auto-update fica desligado) — isso é seguro e evita que o OctoPrint bata na API do GitHub para um repositório inexistente.

Para habilitar auto-update:

1. Crie o repositório público no GitHub.
2. Edite as duas constantes `GITHUB_USER` e `GITHUB_REPO` em `octoprint_marketplace_link/__init__.py`.
3. Faça uma release no GitHub com a tag `v<versão>` (ex.: `v0.3.0`).
4. Builde um novo wheel/sdist e publique. O OctoPrint passa a oferecer atualização quando uma tag mais nova aparecer.

---

## Build a partir do source (para desenvolvedores)

```bash
git clone https://github.com/matheusprtlh/Plugin-Octoprint
cd Plugin-Octoprint/plugin

# Instale o `build`
python -m pip install build

# Gere o wheel + sdist em dist/
python -m build

# Resultado:
# dist/octoprint_marketplacelink-0.3.0-py3-none-any.whl
# dist/octoprint_marketplacelink-0.3.0.tar.gz
```
