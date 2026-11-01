# A Ponte — Central de Salas (A2A v1.0 + MCP 2026-07-28)

Implementação da ponte entre o protocolo **A2A v1.0** (`SendMessage` / `GetTask` / Agent Card) e o protocolo **MCP `2026-07-28`** sobre **Streamable HTTP stateless** com **MRTR (`input_required`)** e **`RequestStateSecurity`** usando o SDK oficial `mcp==2.3.0`.

---

## 1. Como rodar

### Pré-requisitos

- **Python** `>= 3.10` (recomendado Python `3.11+`) com suporte a `venv` e `pip`.
- Dois terminais independentes para executar o servidor MCP e o agente A2A em processos separados.

### Criação do ambiente virtual e instalação com versões travadas

Na raiz do repositório (`desafio-a2a-com-mcp/`), crie um ambiente virtual limpo e instale as dependências com versões travadas (`mcp==2.3.0` em ambos os processos):

```bash
python3 -m venv .venv
.venv/bin/pip install -r servidor-mcp/requirements.txt -r agente/requirements.txt
```

### Geração e exportação do segredo (`REQUEST_STATE_SECRET`)

Gere uma chave hexadecimal criptograficamente segura de 32 bytes (64 caracteres hexadecimais) com o comando:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Exporte o valor gerado na variável de ambiente `REQUEST_STATE_SECRET` no terminal do servidor MCP (não utilize valores de exemplo fixos):

```bash
export REQUEST_STATE_SECRET="<cole-aqui-os-64-caracteres-hex-gerados>"
```

### Inicialização dos processos

Os comandos abaixo utilizam explicitamente o interpretador Python do ambiente virtual (`.venv/bin/python`). O projeto não depende do bloco `os.execv` presente nos scripts para funcionar (esse bloco existe apenas como conveniência caso o usuário invoque `python3` fora do `.venv` ativado).

**Terminal 1 — Servidor MCP (porta padrão `7301`, endpoint `/mcp`):**

```bash
export REQUEST_STATE_SECRET="<mesmo-segredo-de-64-caracteres-hex>"
export MCP_PORT=7301
.venv/bin/python servidor-mcp/main.py
```

**Terminal 2 — Agente A2A / Host MCP (porta padrão `7300`, endpoints `/.well-known/agent-card.json` e `/a2a`):**

```bash
export A2A_PORT=7300
export MCP_URL="http://localhost:7301/mcp"
.venv/bin/python agente/main.py
```

### Configuração de portas (`MCP_PORT`, `A2A_PORT`, `PORT` e `MCP_URL`)

- **Portas padrão:** o servidor MCP escuta por padrão na porta **`7301`** (`resolver_porta_mcp()`) e o agente A2A escuta por padrão na porta **`7300`** (`resolver_porta_a2a()`).
- **Precedência e fallback determinístico:** variáveis específicas (`MCP_PORT` e `A2A_PORT`) sempre têm precedência sobre a variável genérica `PORT`, que nunca é aplicada simultaneamente aos dois processos:
  1. `MCP_PORT` sempre determina a porta do servidor MCP quando definida.
  2. `A2A_PORT` sempre determina a porta do agente A2A quando definida.
  3. Quando somente `PORT` está definida, ela atua como fallback do servidor MCP e o agente A2A permanece em `7300`.
  4. Quando `MCP_PORT` está definida e `A2A_PORT` não está, `PORT` atua como fallback do agente A2A.
  5. Quando `A2A_PORT` está definida e `MCP_PORT` não está, `PORT` atua como fallback do servidor MCP.

| Configuração | Porta MCP | Porta A2A | URL padrão no `ClienteMCP` |
|---|---:|---:|---|
| Nenhuma variável definida | `7301` | `7300` | `http://localhost:7301/mcp` |
| Somente `PORT=9000` | `9000` | `7300` | `http://localhost:9000/mcp` |
| `MCP_PORT=7301`, `PORT=9000` | `7301` | `9000` | `http://localhost:7301/mcp` |
| `A2A_PORT=7300`, `PORT=9000` | `9000` | `7300` | `http://localhost:9000/mcp` |
| `MCP_PORT=7301`, `A2A_PORT=7300`, `PORT=9000` | `7301` | `7300` | `http://localhost:7301/mcp` |
| `MCP_PORT=9000`, `A2A_PORT=9000` | Colisão (erro) | Colisão (erro) | — |

- **Endereço do servidor MCP (`MCP_URL`):** quando `MCP_URL` não é definida, `ClienteMCP` utiliza automaticamente `http://localhost:<porta_mcp>/mcp` com a mesma porta resolvida para o servidor MCP. `MCP_URL` pode sobrescrever esse endereço para apontar tanto para outra porta local quanto para um servidor MCP remoto (ex.: `https://outro-servidor.example:7300/mcp`).

### Execução do validador oficial

Com os dois processos recém-iniciados (pois o validador modifica reservas em memória durante a bateria de verificações), execute em um terceiro terminal:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

### Notas para o avaliador

- **Chamadas diretas ao MCP com curl:** enviar obrigatoriamente os headers:
  - `MCP-Protocol-Version`
  - `Mcp-Method`
  - `Mcp-Name`, quando aplicável (`tools/call` e `resources/read`)
  - `Accept: application/json, text/event-stream`

  O servidor não preenche esses headers automaticamente.

- **Teste de restart:** ao reiniciar o servidor MCP durante uma elicitation em aberto, utilize exatamente o mesmo `REQUEST_STATE_SECRET` para que o `RequestStateSecurity` consiga autenticar e decifrar o `requestState` na retomada.

---

## 2. Onde a ponte acontece

Quando o cliente A2A envia um pedido de reserva via `SendMessage`, o método `GerenciadorTasksA2A.processar_send_message` em `agente/main.py` invoca a ferramenta `reservar_sala` no servidor MCP por meio de `ClienteMCP.chamar_tool` e inspeciona o campo `result.resultType`: se o servidor MCP responder com `resultType: "input_required"`, a função auxiliar `_extrair_dados_input_required` em `agente/main.py` extrai a chave da elicitation em `inputRequests`, a lista ordenada de salas do `requestedSchema` (`enum`/`const`) e o token `requestState`, permitindo que `processar_send_message` converta `input_required` do MCP para o estado `TASK_STATE_INPUT_REQUIRED` da Task A2A (com a mensagem `alternativas: <id1>, <id2>`) e armazene o contexto em `self._contextos_mrtr[task_id]`. Quando o cliente A2A envia um novo `SendMessage` referenciando o `taskId` pausado com `escolha=<sala>` ou `escolha=recusar`, o método `GerenciadorTasksA2A._processar_continuacao_mrtr` em `agente/main.py` recupera o contexto daquela Task e executa uma nova requisição `tools/call` em `ClienteMCP.chamar_tool` com um **ID JSON-RPC novo**, enviando `inputResponses` e devolvendo o `requestState` ao servidor MCP; durante todo esse ciclo, o `requestState` é estritamente opaco para o agente, sendo apenas armazenado em memória privada e ecoado de volta ao servidor MCP sem nenhuma leitura, interpretação ou modificação de seu conteúdo.

---

## 3. Decisões técnicas

**Proteção do `requestState`**
- Utiliza `RequestStateSecurity` do SDK oficial (`mcp==2.3.0`) configurado no `MCPServer` em `servidor-mcp/main.py`.
- Criptografia autenticada **AES-256-GCM** e derivação de chave **HKDF-SHA256** (info `"mcp-request-state-v1"`), conforme implementado pelo SDK em `mcp/server/_request_state.py`.
- **TTL de 600 segundos** (`REQUEST_STATE_TTL_SECONDS = 600.0`).
- **Binding criptográfico** por método (`tools/call`), nome da ferramenta (`reservar_sala`), digest SHA-256 canônico dos argumentos (`arg_digest`) e `audience`.
- Qualquer adulteração no token `requestState` ou nos argumentos enviados no retry é rejeitada pelo `RequestStateBoundary` com erro JSON-RPC **`-32602`** (`Invalid params`, HTTP `400`).

**Estrutura interna do estado**
- O middleware `preservar_alternativas_mrtr` em `servidor-mcp/main.py` intercepta resultados `input_required` antes da selagem AEAD e armazena a lista validada de `alternativas` ofertadas dentro do dicionário de estado.
- Os argumentos originais (`sala`, `inicio`, `fim`, `responsavel`) permanecem vinculados criptograficamente por digest (`arg_digest`) no AAD do AES-GCM, não sendo incorporados como valores de negócio em texto claro no payload do estado.
- Tanto `_extrair_alternativas_do_request_state` quanto `preservar_alternativas_mrtr` aplicam validação estrita da estrutura interna gerada pelo SDK (`type(v) is int and v == 3`, `outcomes` como `dict` e `asked` como `dict` com exatamente 1 entrada), operando em modo *fail-closed* caso o formato divirja.
- No código-fonte do SDK `mcp==2.3.0` (`mcp/server/mcpserver/resolve.py`, linha `97`), a constante interna é `_STATE_VERSION = 3`, e o próprio método `_decode_state` (linha `779`) descarta qualquer estado cujo campo `v` difira de `_STATE_VERSION`. Por isso, a versão é mantida travada de forma exata (`mcp==2.3.0`) em `servidor-mcp/pyproject.toml`, `servidor-mcp/requirements.txt`, `agente/pyproject.toml` e `agente/requirements.txt`, sem operadores `>=`, `~=` ou `^`.

**Limitação do SDK e contorno**
- No `mcp==2.3.0`, o resolver declarado em `Resolve(...)` é reexecutado do zero a cada rodada de `tools/call`. Conforme documentado na docstring de `resolve_arguments` em `mcp/server/mcpserver/resolve.py` (linhas `488–491`): *"Each question is asked once - its answer is carried in `request_state` across rounds and satisfies the question when the resolver asks it again. Resolver bodies themselves may re-run on each round; a recorded answer is consulted only when the body asks, never in place of running it"*, e o SDK descarta a resposta enviada pelo cliente caso o resolver não retorne novamente um marcador `Elicit` ou caso o digest do schema mude entre a pergunta e a resposta (`"Discarding the answer for resolver ...: the question changed since it was asked"`).
- Em `escolha_de_sala` (`servidor-mcp/main.py`), quando `ctx.request_state` está presente na retomada, o resolver extrai as `alternativas_originais` e a chave da pergunta registrada em `asked` no próprio `request_state`:
  - Quando a resposta para aquela chave é `decline` ou `cancel`, reconstrói o `Elicit` com as `alternativas_originais`, preservando a recusa mesmo após um restart do servidor MCP (quando uma reserva criada em memória antes do restart deixou de existir e a sala original voltaria a parecer livre).
  - Quando a resposta é `accept` e a sala escolhida pertence às `alternativas_originais` e continua livre, reconstrói o `Elicit` original para que o SDK valide a escolha e conclua a reserva.
  - Quando a alternativa escolhida ficou ocupada durante a pausa (ou em um replay sequencial direto no MCP reutilizando o mesmo `requestState` e `inputResponses` após a reserva já ter sido criada), o resolver detecta que a sala escolhida não está mais livre e recalcula as `alternativas_atuais`, fazendo o SDK emitir um novo `input_required` com as alternativas restantes (ou `ToolError` se não restarem alternativas) sem nunca duplicar a reserva. Na camada A2A, qualquer tentativa de reenviar uma continuação para uma Task já concluída é recusada imediatamente com `-32602` por estado terminal.
  - Quando uma chamada direta ao MCP envia `accept` com valor fora do enum original, devolve o `Elicit` original para que a validação de schema do SDK rejeite a resposta com `ToolError` (`isError: true`).

**Estado das Tasks**
- As Tasks A2A são mantidas em memória por `taskId` em `GerenciadorTasksA2A._tasks` (`agente/main.py`).
- O contexto MRTR é armazenado em um dicionário privado e separado (`GerenciadorTasksA2A._contextos_mrtr`), indexado por `taskId`.
- Esse contexto privado guarda exclusivamente `request_state`, `chave_input`, `alternativas`, `argumentos` originais e o `traceparent` original da Task (garantindo que todas as chamadas MCP daquela Task preservem o mesmo `trace-id` original com um `span-id` novo por requisição).
- Nenhuma dessas informações internas de controle é exposta em respostas A2A (`SendMessage`/`GetTask`), mensagens de histórico, Agent Card ou artifacts.

**Por que o agente usa HTTP cru**
- O cliente MCP de alto nível do SDK com callback de elicitation responderia à pergunta diretamente dentro da mesma chamada, impedindo o agente A2A de pausar a Task em `TASK_STATE_INPUT_REQUIRED` e aguardar a decisão do usuário via A2A.
- O cliente HTTP cru (`ClienteMCP` sobre `urllib.request`) permite que o agente receba o envelope `resultType: "input_required"` diretamente e faça a tradução explícita para o ciclo de vida de Tasks A2A.
- Antes da primeira chamada `tools/call` de `reservar_sala`, `ClienteMCP.chamar_tool` executa `tools/list` (`descobrir_tools`) e em seguida `resources/read` de `politica://uso` (`obter_versao_politica`), propagando o `traceparent` original em ambas e armazenando a versão da política em cache antes de qualquer efeito colateral no servidor MCP (evitando que uma falha de leitura do resource ocorra depois de a reserva já ter sido criada).
- A dependência do pacote MCP (`mcp==2.3.0` / `mcp-types==2.3.0`) no agente é utilizada apenas para importar constantes e tipos de metadados do protocolo (`PROTOCOL_VERSION_META_KEY`, `CLIENT_INFO_META_KEY`, `CLIENT_CAPABILITIES_META_KEY`).

**Reservas**
- As salas, a política de uso e as reservas iniciais são carregadas dos arquivos em `dados/` na inicialização de `EstadoDominio` (`servidor-mcp/main.py`), e todas as novas reservas são mantidas exclusivamente em memória.
- Não há persistência em disco nem banco de dados.

**Configuração das portas**
- Ambos os processos validam portas no intervalo `1–65535` e seguem a mesma regra determinística de precedência (`MCP_PORT` para o MCP, `A2A_PORT` para o A2A e `PORT` como fallback exclusivo do processo cuja variável específica não foi definida).
- Colisões entre portas locais são detectadas antes de iniciar o servidor HTTP, encerrando o processo com mensagem de erro clara no `stderr` e código `1`.
- Quando `MCP_URL` aponta para um servidor remoto (qualquer host fora de `localhost`, `127.0.0.1`, `::1` e `0.0.0.0`), `resolver_porta_a2a()` não compara a porta remota com a porta local do agente, evitando falsos positivos de colisão quando o servidor MCP remoto utiliza a mesma numeração de porta (ex.: `7300`).

**Riscos técnicos conhecidos**
1. A verificação de conflito (`buscar_conflitos`) e a criação da reserva (`criar_reserva`) utilizam operações com locks separados em `EstadoDominio`. Não existe garantia de atomicidade entre duas requisições concorrentes disputando a mesma sala no mesmo instante.
2. O `requestState` expira após 600 segundos (`REQUEST_STATE_TTL_SECONDS = 600.0`). Uma continuação enviada após a expiração do TTL é rejeitada pelo `RequestStateBoundary` com `-32602`.

---

## 4. Saída do validador

```text
trace-id desta execucao: 3db49b1324353ea53b5aaccc60a0bbbe
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
