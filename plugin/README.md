# OctoPrint-MarketplaceLink

Plugin OctoPrint que envia o estado da impressora para um **webhook configurável** a cada evento relevante (conexão, início/pausa/fim de impressão, erro). Sem servidor central — você aponta o plugin para qualquer URL HTTP que aceite POST de JSON.

Suporta dois modos:

- **Modo serial (padrão)** — qualquer impressora que o OctoPrint enxergue via porta serial (Marlin, Klipper, RepRap, Prusa, Creality, Elegoo, etc.).
- **Modo Bambu Lab** — para impressoras Bambu Lab (A1 / A1 Mini / P1S / P1P / X1C). Como elas não aparecem no OctoPrint via serial, o plugin conecta direto no broker MQTT local da impressora e traduz o estado para os mesmos eventos que o modo serial emitiria. **Mesmo payload, mesmo webhook** — quem recebe não diferencia.

- Versão: **0.4.0**
- Identificador: `marketplace_link`
- Compatibilidade: Python 3.9–3.12 / OctoPrint 1.4+
- Licença: AGPLv3
- Dependências: `requests>=2.28`, `paho-mqtt>=1.6` (instaladas automaticamente)

---

## Instalação

### Opção 1 — Via Plugin Manager (a partir de URL do GitHub)

1. No OctoPrint, abra **Settings → Plugin Manager → Get More**.
2. Clique em **... from URL** e cole:
   ```
   https://github.com/matheusprtlh/Plugin-Octoprint/archive/v0.4.0.zip
   ```
3. Confirme a instalação e reinicie o OctoPrint quando ele pedir.

### Opção 2 — Via wheel local (instalação offline)

Recebeu o arquivo `octoprint_marketplacelink-0.4.0-py3-none-any.whl` (ou o `.tar.gz`)?

```bash
# Active o venv onde o OctoPrint está rodando
source ~/oprint/bin/activate                    # caminho típico no OctoPi

# Instale o pacote (puxa paho-mqtt automaticamente)
pip install octoprint_marketplacelink-0.4.0-py3-none-any.whl

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
| **Marca** | `creality`, `elegoo`, `prusa`, `bambu`, `other`. Apenas metadado, repassado no payload. **Quando o "Modo Bambu Lab" está habilitado, o campo `printer.brand` é forçado para `"bambu"` no payload** independente do que estiver selecionado aqui. |
| **Modelo** | Texto livre. Ex.: `Ender 3 V2`. No modo Bambu o campo `Modelo Bambu` (dropdown) tem prioridade quando preenchido. |
| **Heartbeat (min)** | Intervalo entre postagens periódicas (`event: "heartbeat"`) para indicar que a impressora continua viva. `0` desativa. Padrão: 5 min. |
| **Modo Bambu Lab** | Habilita o bridge MQTT direto para Bambu (A1/P1S/P1P/X1C). Veja a seção [Modo Bambu Lab](#modo-bambu-lab-mqtt-direto) abaixo. |

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

## Modo Bambu Lab (MQTT direto)

Impressoras **Bambu Lab** (A1, A1 Mini, P1S, P1P, X1 Carbon) têm firmware fechado e **não aparecem para o OctoPrint via porta serial**. A partir da versão 0.4.0 o plugin tem um modo dedicado: ele se conecta direto no broker MQTT local da impressora (porta `8883`, TLS) e traduz o estado da impressora nos mesmos eventos que o modo serial emitiria. **O webhook que você configurou recebe o mesmo payload em qualquer modo** — só o campo `printer.brand` muda para `"bambu"`.

### O que dá pra fazer

- **Somente leitura**. O plugin observa o estado da Bambu e dispara `Connected`, `PrintStarted`, `PrintPaused`, `PrintResumed`, `PrintDone`, `PrintFailed`, `Disconnected`. Também repassa `job.file` (nome do `.3mf`), `job.progress` (em %), e `printer.firmware_version` (versão detectada via `info.module`).
- **Não envia comandos** para a Bambu (não inicia/cancela/pausa). Isso é por design — o escopo deste plugin é reportar estado, não controlar.
- **Roda na mesma instância de OctoPrint** que você já usa. Não precisa de daemon separado, contêiner extra, nem máquina dedicada.

### Como configurar

1. **Descubra os 3 dados na tela da impressora:**
   - **IP local**: na tela da Bambu, vai em **Settings → WLAN**. Anote o IP (ex.: `192.168.0.123`). **Recomendação forte**: reserve esse IP no DHCP do roteador, senão da próxima vez que a impressora reiniciar pode pegar IP diferente e o plugin para de conectar.
   - **Serial**: em **Settings → Device Info**. Algo como `01PS00A0B0000001`.
   - **Access Code**: em **Settings → WLAN → "Access Code"**. São **8 dígitos**. Esse é o "password" do MQTT local da impressora.

2. **Abra Settings → Marketplace Link no OctoPrint:**
   - Marque **"Habilitar modo Bambu Lab"**.
   - Preencha **Host**, **Serial**, **Access Code**.
   - Selecione o **Modelo Bambu** no dropdown (A1 / A1 Mini / P1S / P1P / X1 Carbon).
   - Clique em **"Testar conexão Bambu"**. Se aparecer "OK — broker MQTT respondeu, credenciais aceitas", está pronto. Se aparecer erro, veja a tabela de troubleshooting abaixo.

3. **Salve.** O plugin reinicia o bridge automaticamente. O campo "Status atual" vai pra `idle` (ou `printing` se já estava imprimindo). A partir desse ponto, o webhook configurado começa a receber eventos da Bambu igualzinho como receberia de uma Creality.

### Compatibilidade de firmware

Este é o ponto **mais frágil** dessa integração. A Bambu vem **bloqueando o acesso MQTT local** em versões recentes de firmware — começando pela A1 a partir da `01.07.x`, com cortes similares na P1S e X1C. Em firmwares afetados, o broker MQTT requer **Developer Mode** habilitado manualmente na impressora (ou rollback do firmware).

| Modelo | Firmware confirmado funcionando | A partir de | Observação |
|---|---|---|---|
| A1 / A1 Mini | `01.06.x.x` e anteriores | `01.07.00.00` | LAN-only mode foi restringido — exige Developer Mode |
| P1S / P1P | `01.06.x.x` e anteriores | `01.07.00.00` | Mesmo comportamento |
| X1 Carbon | `01.07.x` e anteriores | `01.08.00.00` aprox. | Mesmo comportamento |

**Antes de habilitar o modo Bambu, oriente o usuário a:**

1. **Não atualizar o firmware da Bambu** sem antes verificar se a versão alvo ainda permite LAN mode.
2. Se o firmware já estiver bloqueado, ativar **Developer Mode** em **Settings → General → Developer Mode** (ou fazer rollback — Bambu permite via cartão SD).
3. **Desligar atualização automática** na impressora.

Esses passos são responsabilidade do usuário final — o plugin não tem como forçar o firmware da impressora.

### Troubleshooting

| Mensagem do botão "Testar conexão Bambu" | O que provavelmente está acontecendo | O que fazer |
|---|---|---|
| `Falha de rede: ConnectionRefusedError` ou `gaierror` | IP errado, ou impressora desligada/offline. | Confira o IP, dê ping na impressora, verifique se ela está na mesma rede do OctoPrint. |
| `Timeout aguardando CONNACK` | A porta 8883 está inacessível (firewall, rede separada). | Confira se o OctoPrint e a Bambu estão no mesmo Wi-Fi/VLAN. |
| `Usuário/senha inválidos — confira o access code` | Access Code digitado errado. | Re-leia da tela da Bambu (Settings → WLAN → Access Code). São **8 dígitos**. |
| `Não autorizado — provavelmente o firmware desligou o LAN mode` | Firmware bloqueou LAN mode. | Habilite Developer Mode na impressora, ou faça rollback de firmware. |
| `Protocolo MQTT incorreto` | Firmware muito recente que mudou o handshake. | Mesmo caso acima — Developer Mode ou rollback. |

### Como testar com uma Bambu real

1. Configure como descrito acima.
2. Use um receiver de webhook (ex.: [webhook.site](https://webhook.site) gera uma URL temporária pra você).
3. Cole essa URL no campo **URL** da settings.
4. Clique em **"Testar webhook"** — deve aparecer um POST com `event: "test"`.
5. Mande uma impressão pequena (5 min) pela Bambu Studio ou direto pelo display.
6. No webhook receiver, você deve ver, em ordem: `PrintStarted` → (eventualmente) `PrintDone` (ou `PrintFailed`).
7. Cancele uma impressão no meio para gerar `PrintFailed`.

Se em algum momento o ícone na navbar do OctoPrint ficar **amarelo**, hover nele — o tooltip mostra o último erro. Logs detalhados vão para `~/.octoprint/logs/octoprint.log`, prefixados com `Bambu:`.

### Notas técnicas (para desenvolvedores)

- **TLS**: o broker MQTT da Bambu usa certificado autoassinado. O plugin se conecta com `cert_reqs=CERT_NONE` e `tls_insecure_set(True)`. Isso é seguro **na rede local** (LAN, com credencial de 8 dígitos só visível pra quem tem acesso físico à impressora) — não é seguro para expor à internet. **Não exponha a porta 8883 da impressora**.
- **Tópicos MQTT**: assinamos `device/<serial>/report`. Não publicamos em `device/<serial>/request` — escrita está fora do escopo.
- **Reconexão**: backoff exponencial (1s, 2s, 4s, ..., teto 60s). Falha de auth não fica em loop apertado.
- **Diferenças entre modelos**: A1, A1 Mini, P1S, P1P, X1C reportam o mesmo schema básico (`gcode_state`, `mc_percent`, `subtask_name`/`gcode_file`). Para o reporting de estado dessa integração não há ramificação por modelo. O X1C tem mais campos extras (câmera, AMS HUB, etc.) que ignoramos.

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
