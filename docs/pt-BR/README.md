# c-lord

[![CI](https://github.com/yousan/c-lord/actions/workflows/ci.yml/badge.svg)](https://github.com/yousan/c-lord/actions/workflows/ci.yml)
[![Python 3.10 – 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg)](../../pyproject.toml)
[![MIT license](https://img.shields.io/badge/license-MIT-green.svg)](../../LICENSE)

**Execute várias sessões do Claude Code em paralelo a partir do Discord — cada tópico em seu próprio clone git, de modo que as sessões nunca compartilham uma árvore de trabalho.**

O c-lord transforma um servidor do Discord na sala de controle das sessões do Claude Code que rodam na sua própria máquina. Dê a cada tarefa o seu tópico, afaste-se e acompanhe todas elas pelo celular, pelo notebook ou pelo terminal — nada é interrompido, e tudo o que aconteceu fica registrado no tópico.

<p align="center">
  <img src="../assets/demo-mock.svg" alt="Maquete: um canal do Discord com quatro tópicos do Claude Code, um deles aberto mostrando um pedido, uma mensagem digitada no terminal, a resposta com arquivos e um terminal conectado à mesma sessão (todos os nomes são fictícios)" width="760">
</p>

## Por que c-lord

- 🎛️ **O Discord vira a sala de controle de muitas sessões do Claude Code.** Um tópico é um Claude Code real e interativo rodando no tmux da sua máquina — o mesmo que você usaria em um terminal. Solte arquivos no tópico e o Claude os recebe; os arquivos que o Claude cria voltam como anexos. Quer o teclado? Faça `tmux attach` nessa mesma sessão.
- ⏳ **Sem limite de tempo.** Um trabalho longo nunca é interrompido por demorar. O c-lord só desiste de um turno quando a tela do Claude fica congelada por 5 minutos e, mesmo assim, não mata o Claude.
- 🧪 **Um workspace inteiro e separado por tópico.** Cada tópico trabalha em seu próprio clone do seu repositório, então pode rodar seu backend e seu frontend juntos, e sua própria pilha `docker compose`, sem tocar na de outro tópico. O c-lord sabe quais contêineres pertencem a qual tópico e os para junto com ele. (Ative isso vinculando o canal a um repositório com `/clord-init`; as portas do host continuam compartilhadas, então dê a cada pilha as suas.)
- 📜 **Tudo fica guardado no Discord.** Respostas, atividade de ferramentas, arquivos — até o que você digita diretamente no painel do tmux — vão para o tópico. Sua equipe pode acompanhar, e outro agente de IA pode ler o histórico para continuar o trabalho.
- 🔒 **Você escolhe quem pode conduzi-lo.** Por padrão, só o dono do aplicativo do Discord pode usá-lo (falar com o c-lord executa comandos na sua máquina). Abra-o para um cargo ou para uma lista de pessoas quando quiser.

**Para começar:** [Início rápido](#início-rápido) · [Como funciona](#a-grande-ideia-sessões-paralelas-cada-uma-em-seu-próprio-clone) · [Comandos](../COMMANDS.md) · [Configuração](#configuração) · [Arquitetura](#arquitetura)

[English](../../README.md) · [日本語](../ja/README.md) · [简体中文](../zh-CN/README.md) · [한국어](../ko/README.md) · [Español](../es/README.md) · Português · [Français](../fr/README.md)

Esta página é a tradução do [README em inglês](../../README.md); se os dois textos divergirem, o que vale é o inglês.

> **Baseado no [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge), de [@ebibibi](https://github.com/ebibibi) (Masahiko Ebisuda).** Este projeto foi criado originalmente por ebibibi e é usado aqui sob a Licença MIT. Obrigado pela excelente base.

> O c-lord é um projeto independente de código aberto. A Anthropic não o produz, não o patrocina nem o endossa; "Claude" e "Claude Code" são marcas registradas da Anthropic. O c-lord apenas conduz a CLI do Claude Code que você mesmo instalou e na qual você mesmo fez login.

**Nesta página:** [Como funciona](#a-grande-ideia-sessões-paralelas-cada-uma-em-seu-próprio-clone) · [Início rápido](#início-rápido) · [Trabalhando em um tópico](#trabalhando-em-um-tópico) · [Workspaces](#workspaces) · [Comandos](#comandos) · [Configuração](#configuração) · [Tarefas agendadas](#tarefas-agendadas) · [GitHub e CI](#automação-com-github-e-ci) · [REST API](#rest-api) · [Como biblioteca](#usando-o-c-lord-como-biblioteca) · [Arquitetura](#arquitetura) · [Segurança](#segurança) · [Desenvolvimento](#desenvolvimento) · [Como é construído](#como-este-projeto-é-construído)

## A grande ideia: sessões paralelas, cada uma em seu próprio clone

Cada tópico do Discord é uma sessão própria do Claude Code, rodando em sua própria janela do tmux no seu servidor. Envie tarefas para vários tópicos e elas rodam lado a lado; feche o notebook e elas continuam; responda em um tópico para retomar de onde parou.

O que impede que sessões paralelas atropelem umas às outras é o **isolamento**: quando um canal está vinculado a um repositório (`/clord-init`), cada tópico trabalha em seu próprio `git clone` independente. As sessões nunca compartilham uma árvore de trabalho, então as edições não commitadas de um tópico nunca aparecem em outro. Elas se encontram apenas pelo remoto do git — branches, pushes e PRs — como fazem os contribuidores humanos.

```text
Tópico A (feature)        ──→  Claude Code  (clone próprio)  ─┐
Tópico B (revisão de PR)  ──→  Claude Code  (clone próprio)   ├─→  remoto do git (branches · PRs)
Tópico C (docs)           ──→  Claude Code  (clone próprio)  ─┘
```

**Hoje as sessões não se coordenam entre si.** Versões anteriores deste README descreviam quatro mecanismos de coordenação; este é o estado real deles ([#758](https://github.com/yousan/c-lord/issues/758)):

| Mecanismo | Estado real |
|---|---|
| **Aviso de concorrência** (instruções ao Claude sobre o trabalho em paralelo) | **Não é montado** — as sessões rodam na TUI do tmux (#53), que não tem um canal de system prompt por turno, então ele nunca foi entregue; o c-lord deixou de montá-lo (#766) |
| **Registro de sessões ativas** | Existe em memória e é usado por `/workspace-cleanup`, mas **as sessões não o veem** (nada o envia a elas) |
| **Canal de coordenação** | **Desligado por padrão.** Com `COORDINATION_CHANNEL_ID` definido, publica um aviso de uma linha quando o turno de uma sessão termina; não há evento de início |
| **AI Lounge** | Os endpoints REST funcionam, mas **nada informa ao Claude que o lounge existe**, então na prática nada é publicado — veja [AI Lounge](#ai-lounge-não-entregue-às-sessões) |

Ou seja: árvores de trabalho separadas, não coordenação ao vivo. Tudo o que está fora dos clones — a mesma branch remota, uma porta ou um banco de dados compartilhado, um bot de staging — ainda pode colidir. Dê a cada tópico a sua própria branch.

### AI Lounge (não entregue às sessões)

> **Estado: não conectado** ([#758](https://github.com/yousan/c-lord/issues/758)). O lounge foi projetado para ser injetado em cada sessão como contexto de sistema efêmero. Como as sessões rodam na TUI do tmux (#53), não existe um canal por turno para isso, então o c-lord não o monta (#766): o Claude nunca é informado de que o lounge existe e, na prática, nada é publicado.

Os endpoints REST continuam funcionando, então você mesmo pode usar o lounge — por exemplo, descrevendo-o no `CLAUDE.md` do seu repositório:

```bash
# Publica uma nota (gravada no SQLite; também encaminhada ao canal do lounge, que por padrão é COORDINATION_CHANNEL_ID)
curl -X POST "http://localhost:8080/api/lounge" \
  -H "Content-Type: application/json" \
  -d '{"message": "Starting auth refactor on feature/oauth", "label": "feature dev"}'

# Lê as notas recentes
curl "http://localhost:8080/api/lounge"
```

## Início rápido

Você precisa de uma máquina Linux (WSL2 serve) que fique ligada, com o seguinte instalado:

- Python 3.10 ou mais recente e o [uv](https://docs.astral.sh/uv/)
- `git` e `tmux`
- a [CLI do Claude Code](https://docs.anthropic.com/en/docs/claude-code), já com login feito — execute `claude` uma vez à mão para ter certeza
- um servidor do Discord ao qual você tenha permissão para adicionar um bot

### Passo 1 — Crie o bot do Discord

1. Abra o [Discord Developer Portal](https://discord.com/developers/applications) e crie uma **New Application**.
2. Na página **Bot**, ative **Message Content Intent** (em *Privileged Gateway Intents*) e copie o **token** do bot.
3. Em **OAuth2 → URL Generator**, marque os escopos `bot` e `applications.commands` e, em seguida, estas permissões do bot:

   | Permissão | Usada para |
   |---|---|
   | View Channel (Ver canal) | simplesmente enxergar o canal (veja a nota abaixo) |
   | Send Messages (Enviar mensagens), Send Messages in Threads (Enviar mensagens em tópicos) | respostas |
   | Create Public Threads (Criar tópicos públicos) | um tópico por sessão |
   | Read Message History (Ver histórico de mensagens) | recuperar as mensagens publicadas enquanto o bot estava fora |
   | Add Reactions (Adicionar reações), Manage Messages (Gerenciar mensagens) | a lâmpada de status na sua mensagem, e a remoção dela |
   | Manage Webhooks (Gerenciar webhooks) *(opcional)* | mensagens dos subagentes com o nome de cada um (`CLORD_SUBAGENT_VOICES=1`) |

4. Abra a URL gerada e adicione o bot ao seu servidor.

> **Canais privados / com permissões restritas:** se um canal nega **View Channel**
> a `@everyone` (um canal privado), o convite para o servidor inteiro feito acima *não* basta
> — a negação no nível do canal, no Discord, prevalece sobre ele. Você precisa adicionar o **cargo do bot**
> (ou o próprio bot como membro) às permissões específicas desse canal e conceder pelo menos
> **View Channel**, **Create Public Threads**, **Send Messages** e **Send
> Messages in Threads**. Conceder apenas "Manage Threads" **não** resolve.
>
> Quando essas permissões faltam ao bot em um canal, **o `/clord` responde (de forma efêmera)
> com as permissões exatas a conceder** (#443) — um comando de barra chega ao bot
> quer ele enxergue o canal, quer não. Corrija as permissões antes de continuar:
> sem **View Channel**, o bot não recebe nenhuma mensagem desse canal, então
> o que você escrevesse em um tópico ali nunca chegaria ao Claude.

### Passo 2 — Execute o assistente de configuração

O assistente grava o `.env` para você; não há nada para editar à mão.

```bash
# Direto do GitHub, sem instalar nada:
uvx --from "git+https://github.com/yousan/c-lord.git" c-lord setup

# Ou a partir de um clone (é também o que você quer para rodá-lo como serviço):
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv run c-lord setup
```

Ele pergunta seis coisas, nesta ordem:

1. **CLI do Claude Code** — ele apenas verifica se `claude` pode ser executado e avisa se não puder.
2. **Token do bot** — o token é conferido na API do Discord (um token colado como `Bot xxx` é gravado como `xxx`; depois de 3 respostas vazias ou inválidas, ele para sem gravar o `.env`).
3. **Canal** — ele lista os canais de texto que o bot consegue ver; escolha um pelo número (sem copiar IDs) ou cole um ID numérico de canal (uma URL de canal, ou dígitos como `²`, faz a pergunta se repetir). Esse valor vira `DISCORD_CHANNEL_ID`, o canal principal do bot.
4. **Seu ID de usuário do Discord** — opcional; é quem recebe as @-menções. Um nome de usuário no lugar do ID numérico faz a pergunta se repetir, já que `c-lord start` o recusa.
5. **Diretório de trabalho** — o diretório padrão para execuções que não têm um clone próprio.
6. **Modelo** — `sonnet`, `opus` ou `haiku`.

Depois ele grava o `.env` (use `--env PATH` para colocá-lo em outro lugar) e oferece iniciar o bot ali mesmo.

### Iniciar / Parar

```bash
c-lord start    # inicia o bot (lê o .env do diretório atual)
c-lord start --env /path/to/.env   # local personalizado do .env
c-lord version  # mostra o build em execução, p. ex. v1.4.183-bd80c47e-20260908
```

Em um clone, coloque `uv run` na frente desses comandos. Um bot iniciado assim roda em primeiro plano; Ctrl-C o encerra. Para mantê-lo rodando, veja [Executar como serviço](#executar-como-serviço-systemd---user--sobrevive-a-reinicializações).

> **Precedência da configuração (#324):** as chaves definidas no arquivo `.env` sempre prevalecem sobre
> as variáveis de ambiente herdadas do processo — o diretório de onde você inicia
> determina a identidade do bot. Para as chaves *ausentes* do arquivo, o valor ainda vem
> do ambiente do processo, então configurações feitas só com variáveis de ambiente continuam funcionando.

### Passo 3 — Vincule um repositório e inicie uma sessão

As sessões são iniciadas com um comando de barra, nunca por uma mensagem comum digitada no próprio canal (essas são ignoradas, para que o canal possa continuar sendo um lugar normal de conversa).

```text
/clord-init repo:https://github.com/you/your-project.git     ← uma vez por canal
/clord Fix the flaky login test                               ← abre um tópico e inicia o Claude nele
```

O `/clord-init` registra em qual repositório o canal trabalha. Por padrão, o Discord só o oferece aos membros com a permissão **Manage Server** (Gerenciar servidor). A partir daí, cada `/clord` abre um novo tópico, clona esse repositório para ele e inicia o Claude Code no clone. Tudo o que você escreve **dentro desse tópico** vai para a mesma sessão do Claude. Para usar um repositório diferente em um único tópico, indique-o: `/clord repo:<url> <prompt>` — isso também funciona em um canal sem vínculo.

O host clona com as próprias credenciais de git, então um repositório privado precisa de uma chave SSH ou de um credential helper configurado ali. Quando um clone falha, o tópico mostra o erro do próprio git e o que verificar ([especificação](../specs/workspace-setup-failure.md)).

### Executar como serviço (systemd --user — sobrevive a reinicializações)

Para manter o c-lord rodando em segundo plano e **iniciá-lo automaticamente quando o
host reinicia**, instale-o como um serviço `systemd --user`. A partir do seu clone:

```bash
bash scripts/install-systemd.sh
```

Isso instala o [`deploy/c-lord.service`](../../deploy/c-lord.service) sem alterações como
`~/.config/systemd/user/c-lord.service`, grava os valores específicos do host (o caminho deste
clone, o seu `uv`, o seu `PATH`) no drop-in
`c-lord.service.d/10-install.conf`, habilita o serviço e ativa o **linger**, para que o
serviço inicie no boot mesmo sem ninguém logado. Execute-o de novo depois que
`deploy/c-lord.service` mudar. Depois, gerencie-o com o systemd de sempre:

```bash
systemctl --user restart c-lord.service     # depois de um `git pull`
systemctl --user stop    c-lord.service
systemctl --user status  c-lord.service
journalctl --user -u c-lord.service -f      # logs ao vivo
```

Uma vez instalada a unit, **o systemd é a única coisa que inicia o bot deste
clone.** Não o mate para iniciá-lo à mão — o systemd tentaria recriá-lo, esbarraria
no lock de instância única, desistiria (`Start request repeated too quickly`) e
deixaria o bot rodando sem supervisão. O `bash scripts/staging.sh restart|stop` sabe
disso: em um clone para o qual o `WorkingDirectory=` de uma unit de usuário aponta, ele chama
`systemctl --user restart|stop` em vez de matar o processo, e termina com erro (sem
matar nada) quando `systemctl --user` não está disponível. O `bash scripts/staging.sh status`
mostra se cada processo do bot está dentro do cgroup da unit.

Por que `--user` em vez de um serviço de sistema: o c-lord conduz o `tmux` do usuário,
os arquivos de sessão em `~/.claude`, o `uv` e a CLI `claude`, então ele roda melhor como
**você**, com o seu ambiente — um serviço root/de sistema teria de recriar
tudo isso via `User=`/`Environment=`. É o `loginctl enable-linger "$USER"` que
faz um serviço de usuário sobreviver ao logout e iniciar no boot; o instalador o executa
para você.

> **Instalação manual:** copie o [`deploy/c-lord.service`](../../deploy/c-lord.service), corrija
> os dois caminhos `CHANGE-ME` (diretório do clone + `uv`) e então execute
> `systemctl --user daemon-reload && systemctl --user enable --now c-lord.service && loginctl enable-linger "$USER"`.

> **Nota para WSL:** se `systemctl --user` informar `Failed to connect to bus`, a
> sessão D-Bus do usuário não está ativa. Garanta que haja `systemd=true` em `[boot]` no
> `/etc/wsl.conf` e reinicie o WSL (`wsl --shutdown`), ou reinicie o bot
> matando-o e deixando o `Restart=always` recriá-lo (`pkill -f c_lord.main` não é
> seguro com vários clones — mate pelo PID).

## Trabalhando em um tópico

O que segue é o que você realmente vê no Discord. Cada item aponta para a página que define como ele deve se comportar; o índice completo está em [docs/specs/](../specs/README.md), e o guia do dia a dia é o [docs/USER_GUIDE.md](../USER_GUIDE.md).

### Conversando com o Claude

- **Um tópico, uma conversa.** Uma resposta no tópico continua a mesma sessão do Claude Code (`--resume`), com o contexto intacto — hoje, amanhã ou depois de o bot ter reiniciado.
- **Uma nova mensagem interrompe.** Se o Claude ainda estiver trabalhando quando você escrever de novo, o turno em andamento é interrompido e a sua nova instrução é o que passa a rodar. Não é preciso dar `/stop` antes.
- **Parar.** O `/stop`, ou o botão **⏹ Stop** que fica no fim do tópico enquanto um turno roda, para o turno e os comandos de shell que o Claude deixou rodando. A conversa é mantida; a sua próxima mensagem a continua ([especificação](../specs/stop-button.md)).
- **Sem limite de tamanho para o que você envia.** Textos longos colados são entregues inteiros. Se uma mensagem realmente não puder ser entregue, o tópico avisa — ele nunca encerra um turno vazio em silêncio ([especificação](../specs/input-delivery.md)).
- **Arquivos de entrada.** Anexe até 10 arquivos a uma mensagem. Cada um é salvo dentro do clone do tópico (`.clord/attachments/…`, fora do git) e o caminho é informado ao Claude ([especificação](../specs/attachments.md)).
- **Arquivos de saída.** Um arquivo que o Claude entrega é anexado ao tópico com o nome real, até 25 MB por padrão ([especificação](../specs/user-file-delivery.md)).
- **Respostas longas chegam completas.** Uma resposta acima do limite de 2000 caracteres do Discord é dividida em várias mensagens sem quebrar blocos de código ([especificação](../specs/long-reply-split.md)).
- **O terminal continua lá.** Faça `tmux attach` na janela do tópico e digite; o que você digita ali aparece no tópico como seu, e a resposta do Claude vem em seguida, como de costume ([especificação](../specs/mirrored-events.md)).
- **Mensagens publicadas enquanto o bot estava fora do ar não se perdem.** Quando ele reconecta ou reinicia, lê o que chegou aos seus tópicos nesse intervalo e executa, com uma linha explicando o atraso ([especificação](../specs/gateway-backfill.md)).
- **O nome do tópico continua sendo seu** — o c-lord nunca refaz por conta própria o resumo que dá nome a um tópico. O tópico mantém o nome com que foi aberto (só o prefixo `W<N> │` e o número `#<issue>` são acrescentados em volta dele); execute `/thread-rename` no tópico quando quiser um novo resumo da conversa recente (sonnet). `CLORD_AUTO_TOPIC=1` restaura o antigo resumo automático (#705)

### Sabendo o que está acontecendo

- **Status em tempo real** — Uma lâmpada em forma de reação com emoji na sua mensagem: 🟢 em execução enquanto o Claude trabalha, 🟡 aguardando quando é a sua vez (❌ em caso de erro, ⏳ / ⚠️ se ele ficar em silêncio por 10 s / 30 s). As reações continuam respondendo bem sob uso intenso. A lâmpada 🟢/🟡 no nome do tópico vem desligada por padrão (#329); defina `CLORD_THREAD_LAMP=1` para ter essa visão na barra lateral, mais lenta e de consistência eventual (#246)
- **Atividade de ferramentas** — Lida do transcript do próprio Claude Code, não publicada ferramenta por ferramenta: um turno que fica em silêncio mostra uma linha de progresso (`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件` — "trabalhando há 5:56 · 61 ferramentas"; veja *Linha de progresso do turno* abaixo), e as chamadas de ferramentas e as frases de progresso do turno são anexadas à resposta como `progress.txt` (#723)
- **Linha de progresso do turno** — Quando um turno fica em silêncio por 90 s, aparece uma linha em subtexto (`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`), que é atualizada no lugar a cada 15 s e desaparece no instante em que volta a haver saída de verdade; ela diz `⏳ 待機中` ("aguardando") quando até a atividade de ferramentas parou. Quando o Claude escreve uma frase de progresso (a linha "●" do painel), a linha mostra a mais recente no lugar da ferramenta (#883) — essas frases também vão para o `progress.txt`, nunca como mensagens próprias. Nunca é publicada fora de um turno. Desative com `CLORD_TURN_PROGRESS=0` (#539)
- **Uso do contexto** — Cada turno termina com um rodapé de uma linha (`📊 7% context (69k/1.0M) · …`): porcentagem da janela de contexto (tokens de entrada + cache, sem contar a saída), modelo, versões e custo; ele vira um aviso ⚠️ acima de 83,5%, quando a compactação automática pode rodar no turno seguinte (`docs/specs/context-footer.md`)
- **Aviso de compactação** — Quando o Claude Code compacta o contexto, o tópico recebe uma linha `🗜️ コンテキストを圧縮しました` ("contexto compactado") em vez do resumo bruto de continuação (#628)
- **Lâmpada de travamento** — Depois de 30 s sem atividade, a reação na sua mensagem vira ⚠️ (raciocínio estendido ou compactação de contexto também podem ficar quietos assim); nenhuma mensagem é publicada no tópico (#473)
- **Aviso de fim de turno** — Quando o turno acaba, o tópico @-menciona quem o pediu, para que você seja notificado. Um turno que não produziu resposta diz isso, em vez de afirmar que terminou ([especificação](../specs/turn-end-notification.md)).
- **Notificações de timeout** — Um embed com o tempo decorrido e orientações para retomar, emitido só quando o Claude está realmente travado (painel congelado durante toda a janela de tempo *e* não ocioso no prompt); um turno que terminou normalmente nunca o dispara (#541), nem um menu de pergunta/plano que ficou esperando a sua resposta (#751)
- **Limite de uso e login** — Atingir o limite de uso do plano é relatado exatamente como tal, com o horário em que ele é renovado ([especificação](../specs/usage-limit.md)). Quando o login do Claude Code expira, o dono recebe uma única DM com o nome do host e os tópicos que pararam, e pode refazer o login a partir dessa DM ([especificação](../specs/login-alert.md)).
- **📊 Painel Session Status** — Desligado por padrão (#761). Defina `CLORD_SESSION_STATUS_BOARD=1` para manter em `DISCORD_CHANNEL_ID` um painel ao vivo que lista cada sessão como 🟢 Auto-processing / 🟡 Waiting for input. Enquanto está desligado, o bot ao iniciar não publica nada no canal e retira qualquer painel deixado por inicializações anteriores; o aviso de fim de turno `🟡 Claude has finished … @you` no tópico não é afetado. Veja `docs/specs/session-status-board.md`

### Quando o Claude pergunta algo a você

- **Perguntas** — Quando o Claude chama `AskUserQuestion`, as opções aparecem no tópico como botões ou como um menu de seleção, e o Claude segue com o que você escolheu. Os botões continuam funcionando depois de uma reinicialização do bot ([especificação](../specs/ask-menu-lifecycle.md)).
- **Plan Mode** — Quando o Claude chama `ExitPlanMode`, o menu de aprovação que o Claude Code mostra é levado ao Discord da mesma forma que o `AskUserQuestion`: as opções aparecem como botões, e o Claude prossegue com a que você escolher (#251)
- **Pedidos de permissão de ferramentas** — Não aparecem no Discord: o c-lord aceita no painel o pedido de permissão do Claude Code para que o turno continue. O que o Claude pode fazer é decidido pelo modo de permissão / pelas configurações com que a sessão roda, não por um botão por ferramenta

### Dividindo o trabalho

- **Log de subagentes** — O trabalho dividido entre subagentes dentro de um tópico fica no Discord: uma linha `🤖 サブエージェント「…」開始` ("subagente … início") / `終了 · 6:12 · ツール 48 件` ("fim · 48 ferramentas") por execução, com a linha de fim trazendo o `subagent-*.md` (relatório completo, incumbência, chamadas de ferramentas) e a cópia dele na linha de início sendo atualizada enquanto a execução dura. `CLORD_SUBAGENT_VOICES=1` acrescenta as mensagens e o relatório de cada agente sob o nome dele (#971, `docs/specs/subagent-log.md`)

**Quando abrir um novo tópico e quando dividir o trabalho dentro de um** (#969): abra um novo tópico só quando o trabalho precisar de sua própria branch, de seu próprio servidor de desenvolvimento ou de sua própria conversa com você. Para dividir um mesmo trabalho — pesquisa, revisões, triagem — peça ao Claude que use subagentes dentro do tópico: nenhum processo `claude` extra (≈285 MB cada) é iniciado, e o início, o fim e o relatório completo de cada subagente continuam chegando ao tópico ([log de subagentes](../specs/subagent-log.md)). O Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`) é experimental e seus teammates não voltam quando o c-lord reinicia ou retoma a sessão; não use a exibição em painéis divididos (`--teammate-mode tmux`), que acrescenta painéis à janela que o c-lord conduz.

### Skills, modelos, commits

- **Skills** — O `/skill` executa uma das skills do Claude Code em `~/.claude/skills/`. O nome tem autocompletar, e uma skill adicionada com o bot em execução aparece em até um minuto. Em um canal, ele abre um novo tópico; em um tópico, continua aquela sessão.
- **Troca de modelo em tempo de execução** — O `/model` mostra e altera o modelo e o esforço no escopo da sessão, do canal ou global (`include-existing` também muda as sessões abertas a partir do próximo turno delas)
- **Quem pediu um commit** — Os commits que o Claude faz em um tópico levam trailers `Co-authored-by:` do usuário do Discord que pediu aquele turno e do Claude. Isso é feito por um hook do git, não pedindo ao Claude que se lembre; `CLORD_COAUTHOR=0` desliga ([especificação](../specs/commit-coauthors.md)).

## Workspaces

Um **workspace** é tudo o que pertence a um tópico: a sessão do Claude, o clone e qualquer ambiente docker iniciado a partir desse clone.

- **Um clone git por tópico** — Com um canal vinculado a um repositório (`/clord-init`), cada tópico trabalha em seu próprio `git clone` independente; as sessões não compartilham nada além do remoto do git
- **Três níveis de "desligado", cada um incluindo o anterior.** Nenhuma dessas operações apaga o histórico da conversa nem um volume do docker ([vocabulário](../specs/workspace-vocabulary.md)):

  | | O que para | Sozinho | À mão |
  |---|---|---|---|
  | **Dormir** (sleep) | o processo `claude` (o docker continua rodando) | depois de 4 horas ocioso (`CLORD_IDLE_SLEEP_HOURS`) | — a próxima mensagem o acorda sem dizer nada |
  | **Parar** (stop) | + o ambiente docker | depois de 7 dias ocioso (`CLORD_IDLE_STOP_DAYS`) | `/workspace-stop`, desfeito por `/workspace-start` |
  | **Excluir** (delete) | + o próprio clone | depois de 30 dias sem uso, veja abaixo | `/workspace-delete` |

- **Um teto para quantos ficam acordados.** Acima de `CLORD_MAX_RESIDENT_WORKSPACES` (calculado por padrão a partir da memória do host), os workspaces ociosos há mais tempo são postos para dormir. Iniciar um novo nunca precisa esperar ([especificação](../specs/resident-cap.md)).
- **Status do canal** — O `/clord-status` lista as sessões deste canal com o tamanho do diretório, o alvo do `tmux attach` e o comando `claude --resume`; `show_all` inclui as sessões encerradas (no estilo `docker ps -a`). Substitui os antigos `/sessions`, `/session-dirs` e `/resume-info`.
- **Limpeza de workspaces** — O `/workspace-cleanup` recupera os diretórios de clone que nenhuma sessão em execução está usando (`dry_run` para pré-visualizar); um diretório que contém trabalho (alterações não commitadas, commits sem push, arquivos ignorados como o `.env`) nunca é removido, e a resposta diz quais são (#1016)
- **Arrumação de 30 dias, com aviso** — A cada inicialização, os workspaces sem uso há **30 dias** são arrumados. O prazo segue a retenção de transcripts do próprio Claude Code (`cleanupPeriodDays`), então a essa altura o Claude em geral já esqueceu a conversa de qualquer forma. O clone só é removido se não contiver trabalho não commitado; o registro que liga o tópico à sessão é mantido. Um tópico ainda aberto recebe um aviso 🧹 dizendo o que aconteceu e o que sobreviveu, e então é arquivado; um tópico já fechado é arrumado sem publicação, para que continue fechado. Escrever mais tarde em um tópico assim reconecta ao que ainda estiver em disco ou inicia uma nova conversa no mesmo tópico. Detalhes: [Why a thread loses its record](../COMMANDS.md#why-a-thread-loses-its-record-the-30-day-sweep).
- **Os clones compartilham objetos git.** Clones do mesmo repositório tomam emprestado de um único espelho local, então o clone de um novo tópico leva segundos e ocupa pouco disco. Nada muda na forma como você usa o git; `CLORD_GIT_MIRRORS=0` desliga ([especificação](../specs/git-object-mirrors.md)).

## Comandos

Todos os comandos estão descritos em **[docs/COMMANDS.md](../COMMANDS.md)**. Os que você mais vai usar:

| Comando | Onde | O que faz |
|---|---|---|
| `/clord-init repo:<url>` | canal | Vincula o canal a um repositório (sem argumento: mostra os vínculos) |
| `/clord <prompt>` | canal | Abre um tópico e inicia uma sessão; `repo:<url>` escolhe um repositório para esse tópico |
| `/stop` | tópico | Para o turno em execução; a conversa é mantida |
| `/clear` · `/compact` | tópico | Recomeça no mesmo tópico · resume o contexto para liberar a janela |
| `/skill <name> [args]` | canal ou tópico | Executa uma skill do Claude Code |
| `/model` | qualquer lugar | Mostra ou altera o modelo e o esforço da sessão, do canal ou de tudo |
| `/clord-status` | qualquer lugar | As sessões deste canal, com o alvo do `tmux attach` e o comando `claude --resume` |
| `/workspace-stop` · `/workspace-start` · `/workspace-delete` · `/workspace-cleanup` | tópico / qualquer lugar | As operações de [workspace](#workspaces) |
| `/claude-restart` · `/resync` · `/tmux-screenshot` | tópico | Reinicia o processo do Claude · reconecta o tópico ao seu painel · publica uma imagem do painel |
| `/thread-rename` | tópico | Refaz o resumo que dá nome ao tópico |
| `/clord-reattach` | tópico | Reconecta um tópico arrumado ao que ainda está em disco |
| `/version` | qualquer lugar | Qual build está em execução |

A maioria dos comandos de barra também existe como comando de texto com `!` na frente (`!clord`, `!stop`, …). São esses que um webhook pode enviar — o Discord não permite que webhooks usem comandos de barra — e é assim que scripts e outros agentes iniciam trabalho: uma mensagem de webhook que começa com `!clord`, publicada em um canal vinculado a um repositório, abre uma sessão.

## Configuração

Tudo é definido no `.env`. Só as duas primeiras variáveis são obrigatórias. O [.env.example](../../.env.example) é um ponto de partida comentado, não a lista completa: algumas das variáveis das tabelas abaixo não estão nele, e ele traz umas poucas (para as ferramentas de teste e de monitoramento) que não estão listadas aqui.

**Conexão**

| Variável | O que faz | Padrão |
|---|---|---|
| `DISCORD_BOT_TOKEN` | O token do bot | (obrigatório) |
| `DISCORD_CHANNEL_ID` | ID do canal para o chat com o Claude. Como toda configuração de ID aqui (`DISCORD_OWNER_ID`, `COORDINATION_CHANNEL_ID`, `EXPECTED_BOT_USER_ID`), só dígitos ASCII — um link, um nome ou `²` interrompe o `c-lord start` informando o nome e o valor da configuração (#1084); espaços em volta dos dígitos são ignorados (#1194) | (obrigatório) |
| `EXPECTED_BOT_USER_ID` | O ID de usuário do próprio bot. Se o processo fizer login como outro bot, ele encerra na hora — vale a pena definir quando vários bots rodam em um mesmo host | (desligado) |

**Claude Code**

| Variável | O que faz | Padrão |
|---|---|---|
| `CLAUDE_COMMAND` | O executável do Claude Code a ser usado | `claude` |
| `CLAUDE_MODEL` | Modelo das novas sessões. O `/model` o altera em tempo de execução | `sonnet` |
| `CLAUDE_EFFORT` | Esforço de raciocínio passado como `--effort` (`low` / `medium` / `high` / `xhigh` / `max`). Sem definir, nenhuma flag é passada | (padrão da CLI) |
| `CLAUDE_PERMISSION_MODE` | Modo de permissão em que as sessões rodam | `acceptEdits` |
| `CLAUDE_WORKING_DIR` | Diretório para execuções que não trazem um clone próprio (uma tarefa agendada ou um gatilho de webhook sem `working_dir`) | diretório atual |
| `CLORD_SESSION_PLUGINS` | `0` impede o c-lord de deixar de fora o plugin do Discord e os plugins de language server desnecessários ao iniciar uma sessão ([especificação](../specs/session-plugins.md)) | `true` |

**Quem pode usar**

| Variável | O que faz | Padrão |
|---|---|---|
| `DISCORD_OWNER_ID` | ID do usuário autorizado a conduzir o bot, e que é @-mencionado quando o Claude precisa de uma resposta. Sem definir, o bot permite apenas o dono do aplicativo do Discord (#713) | (opcional) |
| `CLORD_ALLOWED_ROLE` | Nome do cargo do Discord cujos membros podem conduzir o bot (OR com `DISCORD_OWNER_ID`) | (opcional) |
| `CLORD_ALLOW_ANYONE` | `1` permite que **todos** os membros do servidor conduzam o bot — o que significa executar comandos de shell no host. Gera um aviso na inicialização | `false` |
| `CLORD_NOTIFY_DENIED` | `0` suspende a DM que o dono do aplicativo recebe quando alguém não autorizado publica em um tópico do bot (o remetente nunca vê nada; a recusa continua sendo registrada no log — #346) | `true` |
| `CLORD_HOST_LABEL` | Como a DM de queda de login chama esta máquina (#1309 — para distinguir um segundo host) | nome do host |
| `CLORD_LOGIN_RELAY` | `0` mantém a DM de queda de login, mas retira a possibilidade de refazer o login a partir dela (link de acesso + resposta com o código, #1310) | `true` |

**Limites e workspaces**

| Variável | O que faz | Padrão |
|---|---|---|
| `MAX_CONCURRENT_SESSIONS` | Máximo de **turnos rodando ao mesmo tempo**. Não é um teto de processos `claude` residentes — o semáforo envolve apenas a execução do turno e é liberado quando o turno termina, enquanto o painel do tmux continua vivo. Para o teto de residentes, veja `CLORD_MAX_RESIDENT_WORKSPACES` (#576). Precisa ser um número inteiro **≥ 1** — `0`, negativos e valores não numéricos interrompem a inicialização informando a chave (#1119). Um número com mais de 18 dígitos é ignorado com um WARNING que informa a chave, e o padrão é usado (#1256). | `3` |
| `CLORD_MAX_RESIDENT_WORKSPACES` | Máximo de workspaces com um `claude` vivo ao mesmo tempo. Acima do teto, os ociosos há mais tempo são postos para dormir; a criação de um novo workspace nunca é bloqueada. Ao contrário dos TTLs de ociosidade, este padrão **depende** do host, então é calculado a partir de `MemTotal` (o limite do cgroup primeiro) em vez de vir como constante. `0` desativa. Veja `docs/specs/resident-cap.md`. | automático (`max(2, MemTotal_GiB × 0.4 / 0.45)`) |
| `SESSION_TIMEOUT_SECONDS` | Timeout de inatividade da sessão. Número inteiro **≥ 1**; caso contrário, a inicialização para informando a chave (#1119). Mais de 18 dígitos é ignorado com um WARNING que informa a chave, e o padrão é usado (#1256) | `300` |
| `CLORD_IDLE_SLEEP_HOURS` | Horas de ociosidade até um workspace dormir (frações são aceitas). `0` desativa | `4` |
| `CLORD_IDLE_STOP_DAYS` | Dias de ociosidade até um workspace ser parado. `0` desativa | `7` |
| `CLORD_ORPHAN_SWEEP_DAYS` | Dias até que um clone limpo e intocado, para o qual nenhum registro aponta, seja removido. `0` desativa | `30` |
| `SESSION_DIR_BASE` | Onde ficam os clones de cada tópico. Não mova um que já existe: o `--resume` é indexado pelo caminho do diretório | `~/.c-lord/<instance>/sessions` |
| `CLORD_INSTANCE` | O `<instance>` acima | o nome do diretório do clone |
| `CLORD_GIT_MIRRORS` | `0` impede que clones de um mesmo repositório compartilhem objetos git | `true` |

**Notificações e eventos**

| Variável | O que faz | Padrão |
|---|---|---|
| `CLORD_OWNER_FALLBACK` | Até onde vai o fallback para o dono nos turnos que nenhum humano pediu (webhook / CI / agendador): `all` (🟡 de fim de turno + pausas + falhas), `blocked` (pausas + falhas — quieto quando funciona, barulhento quando quebra), `off` (nunca, nem nas falhas) | `blocked` |
| `CLORD_EVENT_LOG` | Acrescenta a este arquivo uma linha JSON por fim de turno (`completion` / `blocked` / `failure`), depois que a última resposta é publicada — para que um agente despachante ou a CI faça `tail -F` em vez de consultar o tópico repetidamente. Só no lado do servidor; veja [docs/specs/turn-end-events.md](../specs/turn-end-events.md) (#917) | (desligado) |
| `CLORD_EVENT_WEBHOOK_URL` / `CLORD_EVENT_WEBHOOK_SECRET` | Envia por POST o mesmo evento em JSON a esta URL, com `X-Clord-Signature: sha256=<HMAC-SHA256 of the body>`. Nada é enviado sem o segredo. As falhas são repetidas algumas vezes e nunca bloqueiam o turno (#917) | (desligado) |
| `CLORD_EVENT_DEFER_MAX_SECONDS` | Por quanto tempo um `completion` é retido enquanto subagentes em segundo plano ainda rodam, antes de ser enviado mesmo assim | `3600` |
| `COORDINATION_CHANNEL_ID` | Canal que recebe um aviso de uma linha quando o turno de uma sessão termina; também é o canal padrão do AI Lounge. Um número mais longo que um id do Discord (20 dígitos) é ignorado com um WARNING de inicialização que informa a chave, como se não estivesse definido (#1256) | (opcional) |

**Subagentes**

| Variável | O que faz | Padrão |
|---|---|---|
| `CLORD_SUBAGENT_LOG` | `0` desliga o log de subagentes: uma linha `🤖 … 開始` / `終了` por execução de um subagente, com a linha de fim trazendo o `subagent-*.md` (o relatório completo, a incumbência e todas as chamadas de ferramentas) e a cópia dele na linha de início sendo substituída enquanto o agente trabalha. Veja [docs/specs/subagent-log.md](../specs/subagent-log.md) (#971) | `true` |
| `CLORD_SUBAGENT_VOICES` | `1` também publica, **sob o nome de quem fala**, as incumbências dadas pelo pai, cada `SendMessage` entre o pai e seus agentes e cada relatório final — para que se possa acompanhar no tópico quem disse o quê a quem. Usa um webhook quando o bot tem Manage Webhooks (não é obrigatório: sem isso, o nome vai na frente do texto). As chamadas de ferramentas ficam no anexo (#971) | `false` |
| `CLORD_SUBAGENT_PROGRESS_SECONDS` | Com que frequência o arquivo de progresso anexado de um subagente em execução é substituído (só quando mudou; mínimo 10) | `60` |

**O que o tópico mostra**

| Variável | O que faz | Padrão |
|---|---|---|
| `CLORD_RENDER_TABLE_IMAGES` | Defina como `1`, `true` ou `yes` para renderizar tabelas GFM (com pipes) como imagens PNG anexadas às mensagens do Discord | (opcional) |
| `CLORD_SHOW_URL_EMBEDS` | Defina como `1`/`true`/`yes`/`on` para deixar o Discord expandir os cartões de OGP/pré-visualização de link das URLs nas respostas do Claude. Desligado por padrão — as respostas ficam compactas (sem cartão de pré-visualização). | `false` |
| `CLORD_AUTO_TOPIC` | Defina como `1`/`true`/`yes`/`on` para deixar o c-lord resumir o nome de um tópico **novo** com um LLM (haiku) na primeira mensagem — o comportamento anterior ao #705. Desligado por padrão: o tópico mantém o nome com que foi aberto, e o `/thread-rename` (sonnet) refaz o resumo sob demanda. | `false` |
| `CLORD_SESSION_STATUS_BOARD` | Defina como `1`/`true`/`yes`/`on` para mostrar o painel 📊 Session Status em `DISCORD_CHANNEL_ID` (#761). Desligado por padrão: uma inicialização não publica nada ali e retira os painéis deixados por inicializações anteriores. O aviso de fim de turno no tópico não é afetado. | `false` |
| `CLORD_THREAD_RETITLE` | Defina como `1`/`true`/`yes`/`on` para renomear um tópico no meio da conversa quando um LLM julgar que o trabalho mudou (#121). Desligado por padrão (#414) — ele renomeava tópicos com facilidade demais. | `false` |
| `CLORD_THREAD_LAMP` | `1` acrescenta uma lâmpada 🟢/🟡 ao nome do tópico (a visão da barra lateral). Desligado por padrão porque o Discord limita com rigor as renomeações de tópicos; a lâmpada de reação na sua mensagem está sempre ligada ([especificação](../specs/thread-lamp.md)) | `false` |
| `CLORD_TURN_PROGRESS` | `0` desliga a linha de progresso do turno; `CLORD_TURN_PROGRESS_QUIET_SECONDS` muda por quanto tempo o tópico precisa ficar em silêncio antes | `true` / `90` |
| `CLORD_COAUTHOR` | `0` deixa de acrescentar trailers `Co-authored-by:` aos commits do Claude | `true` |

**Processo e tmux**

| Variável | O que faz | Padrão |
|---|---|---|
| `CLORD_TMUX_SCREENSHOT_ROWS` | Altura, em linhas, para a qual o `/tmux-screenshot` aumenta a janela antes de capturar. `0` captura como está | `100` |
| `CLORD_INSTANCE_ID` | Distingue instâncias do c-lord que compartilham um mesmo servidor tmux. Defina apenas quando duas instâncias iniciam do mesmo diretório | o caminho real do diretório de início |
| `CLORD_SHUTDOWN_TIMEOUT_SECONDS` | Depois de SIGTERM/SIGINT, quanto tempo o processo pode levar para sair antes de despejar suas pilhas de chamadas no log e sair com status 70. `0` desativa | `10` |

A REST API tem configurações próprias (`CLORD_API_HOST`, `CLORD_API_PORT`, `CLORD_API_URL`, `CLORD_API_SECRET`, `CLORD_API_ALLOW_ANY_PEER`) — veja [REST API](#rest-api).

### Pré-visualizações de links (CLORD_SHOW_URL_EMBEDS)

Quando a resposta do Claude contém uma URL (p. ex. um link de GitHub Issues), o Discord normalmente a expandiria em um cartão alto de pré-visualização OGP (título, descrição, imagem), empurrando a resposta em si para baixo e poluindo o tópico. Por padrão, o c-lord **suprime** essas pré-visualizações (a flag `SUPPRESS_EMBEDS` é definida em cada resposta), de modo que o link fica no meio do texto e a resposta se lê sem ruído. Defina `CLORD_SHOW_URL_EMBEDS=true` para restaurar a expansão padrão de pré-visualização de links do Discord.

### Renderização de tabelas como imagem (CLORD_RENDER_TABLE_IMAGES)

Quando ativado, as tabelas Markdown com pipes nas respostas do Claude são renderizadas como imagens PNG com o [Pillow](https://python-pillow.github.io/) e anexadas à mensagem do Discord junto com o texto. Cada célula é dividida em trechos de texto e de emoji: **o texto é desenhado com uma fonte com suporte a CJK e os emojis são desenhados em cores** a partir de uma fonte de emoji colorida, de modo que as lâmpadas de status 🟢/🔴 mantêm a cor.

**Todas as mensagens que o espelho publica são cobertas — as atualizações de progresso tanto quanto a resposta final** (#683). O Discord não desenha tabelas Markdown de forma alguma, então uma tabela não renderizada é uma coluna de caracteres `|` crus, e as mensagens intermediárias permanecem no tópico depois que o turno termina. Aplicar isso só à resposta final, portanto, deixava o leitor sem como prever quais tabelas seriam legíveis.

**A imagem vai na mensagem em que a tabela está.** Uma resposta com mais de 2000 caracteres é dividida em várias mensagens; o PNG de cada tabela é anexado à mensagem que contém essa tabela (uma tabela que a divisão corta entre duas linhas é desenhada inteira, na mensagem em que começa). Antes do #750, todas as imagens iam para a *última* mensagem, então uma tabela da primeira metade ficava ali como pipes crus enquanto a imagem dela pendia sob um parágrafo sem relação, mais abaixo. O `progress.txt` continua indo na última mensagem.

**No máximo 10 tabelas por mensagem viram imagens.** Esse é o teto de anexos do Discord, e ultrapassá-lo faz o envio inteiro falhar — texto incluído — então as tabelas depois da décima ficam como markdown cru em vez de custar a mensagem. O limite é por mensagem, não por turno: cada mensagem de progresso tem a sua própria cota. Quando a resposta também leva o `progress.txt`, esse arquivo ocupa uma vaga e as tabelas ficam com as outras nove.

**O markdown inline dentro das células é reduzido a texto simples** antes do desenho — a imagem não pode ser interativa, então deixar a sintaxe crua só vazaria ruído. `[label](url)` / `![alt](url)` viram `label` (a URL não é clicável em uma imagem e só polui), e `**bold**` / `*italic*` / `` `code` `` mantêm apenas o texto interno. A ênfase com sublinhado (`_x_`, `__x__`) é deixada intacta de propósito, para que identificadores como `_is_allowed` / `__init__` não sejam deformados.

**`~~strike~~` é a exceção: é desenhado como um traço de tachado de verdade.** Uma imagem *pode* mostrar uma linha atravessando o texto, e o Claude usa o tachado em tabelas para dizer "esta linha é a que está sendo descartada" — o Discord o risca no corpo da mensagem, então reduzi-lo a texto simples na imagem faria os dois divergirem e inverteria o sentido da tabela. O traço acompanha o texto nas linhas quebradas e vale tanto para parte de uma célula (`~~a~~ b`) quanto para ela inteira.

**Instalação:**

```bash
uv add "c-lord[table]"
# ou: pip install "c-lord[table]"
```

> O mesmo extra `[table]` também alimenta o **`/tmux-screenshot`** — um comando de depuração
> que publica um PNG do painel atual do tmux (com cores ANSI, layout e lâmpadas de status
> preservados). Sem o extra, o comando responde com uma dica de instalação
> em vez de uma imagem.

**Suporte a fontes (texto CJK + emoji colorido):**

O c-lord renderiza texto e emoji separadamente, escolhendo a primeira fonte encontrada em cada grupo:

| Caminho do arquivo de fonte | Grupo / Observações |
|---|---|
| `~/.local/share/fonts/NotoSansJP.ttf` | **Texto** — recomendada para japonês |
| `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` | Texto — cobre JP / ZH / KO |
| `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | Texto — último recurso (só latino) |
| `~/.local/share/fonts/NotoColorEmoji.ttf` | **Emoji colorido** — 🟢🔴🟡✅⚠️ são renderizados em cores |
| `/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf` | Emoji colorido — caminho alternativo do sistema |
| `~/.local/share/fonts/NotoEmoji-Regular.ttf` | Emoji monocromático — fallback se não houver fonte colorida (sem cor, mas sem tofu) |

Se não houver nenhuma fonte de emoji colorida, o c-lord recorre a uma fonte de emoji monocromática (formas dos glifos, sem cor) e, depois, ao desenho do caractere cru. Use a Noto Color Emoji em **CBDT** — o Pillow a renderiza com `embedded_color=True`.

> **Está vendo quadrados vazios (□) no lugar do texto em japonês em uma captura de tela?** Isso é uma
> fonte ausente *nesta* máquina/neste usuário, não um bug — nenhum dos caminhos acima
> foi encontrado, então a renderização recorreu a uma fonte só latina. A instalação é por usuário
> e por host (`~/.local/share/fonts/`), então não é levada para outra
> conta de usuário Linux nem para um host novo, mesmo que já funcione
> em outro lugar.
>
> **Se você não se sente à vontade para executar comandos no terminal, tudo
> bem — basta entregar o bloco abaixo ao seu assistente de IA** (a sua sessão do Claude Code
> ou o próprio tópico do Claude no c-lord) e pedir que ele o execute para você.
> Ele foi escrito para que uma IA possa executá-lo sem supervisão com segurança: só grava no
> seu próprio diretório home, não precisa de `sudo` e não toca em nada fora de
> `~/.local/share/fonts/`.

**Instalação rápida para japonês + emoji colorido (Linux):**
```bash
mkdir -p ~/.local/share/fonts
# Texto em japonês
curl -Lo ~/.local/share/fonts/NotoSansJP.ttf \
  https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJK-Regular.ttc
# Emoji colorido (CBDT)
curl -Lo ~/.local/share/fonts/NotoColorEmoji.ttf \
  https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf
fc-cache -fv
```

As células longas são quebradas em uma largura limitada (`MAX_COL_WIDTH`, que leva em conta a largura de exibição, de modo que CJK conta em dobro) para que tabelas largas continuem legíveis no celular.

> **Nota para outros idiomas CJK (chinês, coreano, árabe, tailandês etc.):** instale uma fonte que cubra a escrita desejada (p. ex. Noto Sans SC para chinês simplificado, Noto Sans KR para coreano) e acrescente o caminho dela à lista `_JP_FONT_PATHS` em `c_lord/discord_ui/table_renderer.py`. Contribuições que acrescentem detecção de fontes para vários idiomas são bem-vindas.

## Tarefas agendadas

Uma tarefa agendada é um prompt que o c-lord executa para você em um intervalo fixo. O c-lord guarda apenas o *quando*; tudo o que diz respeito ao *o quê* — o que verificar, o que publicar, como não se repetir — está no prompt. As tarefas ficam no SQLite e são adicionadas, alteradas e removidas pela [REST API](#rest-api) com o bot em execução, sem mudança de código nem reinicialização. A forma usual de criar uma é pedir ao Claude, em um tópico, que a registre; o Claude tem o endereço da API em `CLORD_API_URL`:

```bash
curl -X POST "$CLORD_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "weekly-deps",
        "prompt": "Check for outdated dependencies and open an issue if there are any",
        "interval_seconds": 604800,
        "channel_id": 123456789012345678
      }'
```

`name`, `prompt`, `interval_seconds` e `channel_id` são todos obrigatórios, e o canal precisa estar vinculado a um repositório com `/clord-init`; `working_dir` e `run_immediately` são opcionais. `PATCH /api/tasks/{id}` com `{"enabled": false}` pausa uma tarefa sem excluí-la. Um único loop acorda a cada 30 segundos e inicia o que estiver vencido; uma tarefa cuja execução anterior ainda está em andamento é pulada, não duplicada.

Cada execução publica uma mensagem `🔄 **[Scheduled]** <task name>`, abre um tópico nela, cria uma janela do tmux no `working_dir` da tarefa e roda o Claude ali — a resposta chega ao tópico como em qualquer outro turno. Como deve ser uma execução agendada (e o que conta como bug) está descrito em [docs/specs/scheduled-tasks.md](../specs/scheduled-tasks.md).

## Automação com GitHub e CI

Qualquer coisa capaz de publicar em um webhook do Discord pode passar trabalho ao Claude. Há duas formas, conforme quem escreve o prompt.

**Quem envia escreve o prompt.** Uma mensagem de webhook que começa com `!clord`, em um canal vinculado a um repositório, abre uma sessão exatamente como o `/clord` faria. Ter a URL do webhook é a permissão, então trate a URL como um segredo.

**O prompt é fixado do seu lado.** O `WebhookTriggerCog` associa um prefixo de mensagem a um prompt que você definiu no código. O webhook só pode escolher *qual* gatilho dispara; não pode fornecer texto para o Claude agir. Essa é a escolha mais segura para CI. O cog não é registrado por padrão — adicione-o no [seu próprio bot](#usando-o-c-lord-como-biblioteca):

```python
from c_lord import ClaudeConfig, WebhookTrigger, WebhookTriggerCog

docs_sync = WebhookTrigger(
    prompt="Read the latest commits on main and update the docs to match. Open a PR.",
    working_dir="/srv/checkouts/my-project",
    timeout=900,  # segundos
)

await bot.add_cog(
    WebhookTriggerCog(
        bot=bot,
        runner=ClaudeConfig(model="sonnet"),
        triggers={"🔄 docs-sync": docs_sync},      # prefixo da mensagem → o que executar
        channel_ids={123456789012345678},          # escuta só aqui
        allowed_webhook_ids={234567890123456789},  # só este webhook
    )
)
```

Um `WebhookTrigger` pula as verificações de permissão do Claude Code, a menos que você passe `dangerously_skip_permissions=False`, porque não há ninguém ali para responder a um pedido de permissão — o que é mais um motivo para restringir `channel_ids` e `allowed_webhook_ids`.

**O que acontece em um gatilho:** a mensagem do webhook ganha um tópico, o Claude roda em sua própria janela do tmux no `working_dir` do gatilho (ou no do runner), e a resposta é publicada nesse tópico. Se a janela não puder ser criada (p. ex. o tmux não está disponível no host), a execução não começa: o tópico diz o motivo, a mensagem do webhook recebe ❌ e o dono do bot é mencionado (#629).

### Exemplo: sincronização automática de documentação (implemente no seu próprio bot)

> **Nota:** Este é um padrão que você pode implementar no seu próprio bot — o c-lord não traz fluxos de sincronização de documentação embutidos. Para uma implementação de referência, veja o [EbiBot](https://github.com/ebibibi/discord-bot).

A cada push para `main`, o seu bot pode acionar o Claude Code para:
1. Baixar as alterações mais recentes e analisar o diff
2. Atualizar a documentação em inglês
3. Traduzir para o japonês (ou para quaisquer idiomas de destino)
4. Criar um PR com um resumo bilíngue
5. Habilitar o auto-merge — o merge acontece automaticamente quando a CI passa

**GitHub Actions (adicione ao seu repositório):**

```yaml
# .github/workflows/docs-sync.yml
on:
  push:
    branches: [main]
jobs:
  ask-claude:
    # Os commits de documentação do próprio Claude levam [docs-sync]; sem esta linha, eles disparariam o workflow de novo
    if: "!contains(github.event.head_commit.message, '[docs-sync]')"
    runs-on: ubuntu-latest
    steps:
      - name: Post the trigger to Discord
        env:
          WEBHOOK: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: |
          curl --fail -sS -H "Content-Type: application/json" \
            --data '{"content": "🔄 docs-sync"}' "$WEBHOOK"
```

O lado do bot é o `WebhookTriggerCog` mostrado acima, com `🔄 docs-sync` como gatilho. Quando o PR do próprio Claude entra em `main`, esse push dispararia o workflow de novo. A linha `if:` interrompe o ciclo, desde que o seu prompt diga ao Claude para colocar `[docs-sync]` nas mensagens de commit dele.

### Recebendo o aviso quando um turno termina

Um script que despachou trabalho não precisa ficar consultando o tópico. Com `CLORD_EVENT_LOG` ou `CLORD_EVENT_WEBHOOK_URL` definido, o c-lord emite um evento por turno concluído — `completion`, `blocked` ou `failure` — depois que a última resposta foi publicada ([especificação](../specs/turn-end-events.md)).

### Atualizando o bot a partir de um webhook

O `AutoUpgradeCog` é para um bot que tem o c-lord instalado como pacote. Assim como o `WebhookTriggerCog`, é você quem o adiciona:

```python
from c_lord import AutoUpgradeCog, UpgradeConfig

await bot.add_cog(
    AutoUpgradeCog(
        bot,
        UpgradeConfig(
            package_name="c-lord",
            working_dir="/srv/my-bot",
            restart_command=["systemctl", "--user", "restart", "my-bot.service"],
            restart_approval=True,
            slash_command_enabled=True,
        ),
    )
)
```

Quando chega uma mensagem de webhook que começa com `trigger_prefix` (padrão `🔄 upgrade`), o cog abre um tópico e relata cada etapa ali:

1. Executa `uv lock --upgrade-package <package_name>` e depois `uv sync` em `working_dir` (substitua qualquer um dos dois com `upgrade_command` / `sync_command`).
2. Anota quais tópicos têm um turno em execução e então espera o trabalho em andamento terminar — todo cog que expõe uma propriedade `active_count` (o protocolo `DrainAware`) é consultado, os seus inclusive. Depois de `drain_timeout` (300 s), ele para de esperar.
3. Executa `restart_command`. Sem ele, nada é reiniciado e o tópico diz que a atualização está concluída.

Depois da reinicialização, os tópicos anotados na etapa 2 recebem um aviso discreto de uma linha dizendo que o c-lord reiniciou. O Claude **não** recebe um novo prompt: os processos `claude` vivem no tmux e nunca foram parados, então não há nada a retomar (#406).

`upgrade_approval=True` pergunta antes da etapa 1, e `restart_approval=True` pergunta depois que a nova versão está instalada, antes das etapas 2 e 3; qualquer uma das duas é respondida com uma reação ✅ no tópico da atualização ou com o botão Approve. `slash_command_enabled=True` acrescenta o `/upgrade`, que inicia o mesmo pipeline sem webhook. Só os usuários autorizados a conduzir o bot podem executá-lo. Ele funciona em um canal ou em um tópico — executado em um tópico, o tópico da atualização é aberto no canal pai — e, se já houver uma atualização em andamento, ele avisa.

Um c-lord que roda a partir do próprio clone não precisa de nada disso: `git pull && systemctl --user restart c-lord.service`.

## REST API

O plano de controle do c-lord para notificações, tarefas agendadas e criação de
sessões. Ela sobe em toda execução — não está ligada à forma como as respostas são entregues
(#712/#543). Requer aiohttp:

```bash
uv add "c-lord[api]"
```

Ela escuta em `CLORD_API_HOST:CLORD_API_PORT` (padrão `127.0.0.1:8080`). Com
`CLORD_API_PORT` **não definido**, uma porta ocupada não é problema: a API passa para a
próxima livre (8081, 8082, … depois qualquer porta livre) e registra no log para onde foi, de modo que dois
clones em um mesmo host ganham cada um a sua API sem nada a configurar (#258). Uma porta que você
**definiu** é usada como está: se estiver ocupada, o bot registra um WARNING e roda sem a
API em vez de falhar ao iniciar. Um valor que não seja de dígitos ASCII (`²`, uma palavra)
é ignorado com um WARNING que cita `CLORD_API_PORT`, como se não estivesse definido; espaços
em volta dos dígitos são ignorados (#1218). Um número acima de 65535 é uma porta que você definiu e que
não pode ser usada: um WARNING cita `CLORD_API_PORT` e o bot roda sem a API
(#1237). Da mesma forma, uma entrada de `CLORD_TRUSTED_BOT_IDS` que não seja de dígitos ASCII, ou
que seja mais longa que um id do Discord (20 dígitos), é ignorada com um WARNING de inicialização, e os outros ids
da lista continuam funcionando. Isso inclui dígitos de largura inteira (`１２３`), que antes eram
lidos como o id 123 — escreva o id em dígitos ASCII.

O Claude de cada sessão é informado de onde está a API pela variável de ambiente
`CLORD_API_URL` — a porta em que ela realmente subiu, de modo que nunca é preciso
mantê-la em sincronia com `CLORD_API_PORT` à mão. Defina `CLORD_API_URL` você mesmo apenas para
apontar as sessões para outro lugar (um proxy reverso, por exemplo). Quando a API não está
rodando, as sessões não recebem `CLORD_API_URL` nenhum.

**Ela só atende o usuário Unix que executa o bot** (#457). `POST /api/spawn` inicia
uma sessão do Claude Code, então alcançar essa porta equivale a um shell como esse
usuário — e o loopback é uma fronteira de *rede*, não de *UID*: todas as contas de um
host multiusuário conseguem se conectar a `127.0.0.1`. Por isso o UID do par é lido de
`/proc/net/tcp` e comparado com o do próprio bot, inclusive em `/api/health`;
todos os demais recebem `403`. Nada a configurar — as suas próprias chamadas continuam funcionando
como estão. Para deixar entrar outro usuário ou outro host, defina `CLORD_API_SECRET` e
faça com que enviem `Authorization: Bearer …`. Veja [docs/SECURITY.md](../SECURITY.md#rest-api-exposure-712-457).

| Método | Caminho | O que faz |
|---|---|---|
| GET | `/api/health` | Verificação de atividade |
| POST | `/api/spawn` | Abre um novo tópico e inicia o Claude nele com `prompt` (`channel_id` e `thread_name` opcionais). Responde assim que o tópico existe |
| POST | `/api/threads/{thread_id}/messages` | Publica `content` em um tópico |
| POST | `/api/threads/{thread_id}/sleep` | Põe o workspace desse tópico para dormir, como faria a suspensão por ociosidade |
| POST | `/api/notify` | Publica uma notificação (`message`; `title`, `color` e `channel_id` opcionais) |
| POST | `/api/schedule` | Publica uma notificação mais tarde (`message`, `scheduled_at` em ISO 8601) |
| GET | `/api/scheduled` | Lista as notificações que ainda estão aguardando |
| DELETE | `/api/scheduled/{id}` | Cancela uma |
| POST | `/api/tasks` | Registra uma [tarefa agendada](#tarefas-agendadas) |
| GET | `/api/tasks` | Lista as tarefas |
| PATCH | `/api/tasks/{id}` | Altera uma tarefa: `enabled`, `prompt`, `interval_seconds`, `working_dir` |
| DELETE | `/api/tasks/{id}` | Exclui uma tarefa |
| POST | `/api/mark-resume` | Registra um tópico antes de você mesmo reiniciar o bot, para que ele receba o aviso de reinicialização na próxima inicialização |
| GET | `/api/lounge` | Lê as mensagens recentes do AI Lounge (as sessões não são informadas sobre o lounge, então nada é publicado aqui a menos que você configure isso — #758) |
| POST | `/api/lounge` | Publica uma mensagem no AI Lounge (com `label` opcional) — as sessões do c-lord não a chamam por conta própria (#758) |

Todos os campos, códigos de status e casos-limite estão em [docs/COMMANDS.md](../COMMANDS.md#rest-api). Dois exemplos, executados como o próprio usuário Unix do bot, de modo que nenhum cabeçalho é necessário:

```bash
# Inicia uma sessão a partir de um script — a resposta volta com o ID do tópico
curl -X POST http://127.0.0.1:8080/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review the newest open PR and summarise the risks", "thread_name": "PR review"}'

# Avisa o canal principal de que algo aconteceu
curl -X POST http://127.0.0.1:8080/api/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "CI", "message": "Nightly build is green"}'
```

## Usando o c-lord como biblioteca

O c-lord é um pacote Python comum, e o `c-lord start` é apenas um programa construído sobre ele. Para colocá-lo dentro de um bot seu — com os seus próprios cogs ao lado — instale-o e chame `setup_bridge()`:

```bash
uv add git+https://github.com/yousan/c-lord.git
```

```python
import asyncio
import os

from c_lord import ClaudeConfig, setup_bridge
from c_lord.bot import ClaudeDiscordBot


async def main() -> None:
    channel_id = int(os.environ["DISCORD_CHANNEL_ID"])
    owner_id = int(os.environ["DISCORD_OWNER_ID"])

    bot = ClaudeDiscordBot(channel_id=channel_id, owner_id=owner_id)
    config = ClaudeConfig(model="sonnet", permission_mode="acceptEdits")

    async with bot:
        await setup_bridge(
            bot,
            config,
            claude_channel_id=channel_id,
            allowed_user_ids={owner_id},
        )
        # await bot.add_cog(YourCog(bot))   ← os seus próprios cogs entram aqui
        await bot.start(os.environ["DISCORD_BOT_TOKEN"])


asyncio.run(main())
```

O `setup_bridge()` registra os cogs padrão do c-lord — nove com a chamada acima: chat, vínculo canal↔repositório, skills, comandos de workspace, a arrumação de 30 dias, o agendador, o espelho do transcript, o relay de login e o `/version`. Isso é proposital: um recurso padrão acrescentado ao c-lord mais tarde chega ao seu bot quando você atualiza o pacote, sem nenhuma mudança no seu código. O `WebhookTriggerCog` e o `AutoUpgradeCog` não estão entre eles; adicione esses dois você mesmo com `bot.add_cog()`, como mostrado acima.

```bash
uv lock --upgrade-package c-lord && uv sync
```

Use `ClaudeDiscordBot` como a classe do bot — os cogs do c-lord leem coisas que ela carrega, como o canal principal. O ponto de entrada standalone, [`c_lord/main.py`](../../c_lord/main.py), é a referência para o resto: a leitura do `.env`, os locks de instância única, a inicialização da REST API e o encerramento ao receber um sinal.

## Arquitetura

### Como o Discord se mapeia para o tmux

A ideia "tópico do Discord = sessão do Claude Code" se apoia em um layout do tmux. Saber onde um tópico realmente vive facilita muito a depuração e a integração (você pode fazer `tmux attach` para observar uma sessão ao vivo):

| Unidade | tmux | Mapeamento |
|------|--------------|---------|
| Repositório ao qual um tópico está vinculado | sessão do tmux | 1:1 |
| Tópico | janela do tmux | 1:1 |

- **As sessões só nascem em um canal.** O `/clord` abre um tópico *novo*; dentro de um tópico, ele continua a sessão desse tópico, oferece reconectar uma cujo registro foi varrido ou recusa se o tópico nunca foi do c-lord (#551). Um tópico de conversa comum não pode ser transformado em uma sessão do Claude — não existe, de propósito, um comando que adote um. Veja [docs/COMMANDS.md](../COMMANDS.md#chat--sessions).
- **1 repositório = 1 sessão do tmux — não 1 canal.** O nome da sessão vem do repositório ao qual o tópico está vinculado: o dele próprio quando tem um (`/clord-thread-init`, ou `/clord repo:<url>`); caso contrário, o do canal (`/clord-init`); canais sem vínculo recorrem à sessão padrão `clord`. Assim, um canal cujos tópicos estão vinculados a repositórios diferentes fica espalhado por várias sessões, e dois canais vinculados ao mesmo repositório compartilham uma. Onde um dado tópico vive — e por que isso é decidido assim — está detalhado em [docs/specs/tmux-layout.md](../specs/tmux-layout.md).
- **1 tópico = 1 janela do tmux = 1 sessão do Claude Code.** Cada tópico ganha a sua própria janela (`w1`, `w2`, …), que roda uma sessão do Claude Code. Portanto, a sua troca de mensagens em um tópico *é* a troca de mensagens com aquela sessão do Claude — as respostas a continuam via `--resume`. Os números de janela são únicos **dentro de uma sessão**, não dentro de um canal.

Para o "porquê" deste design, veja [docs/PHILOSOPHY.md](../PHILOSOPHY.md).

### Como uma resposta chega ao Discord

```text
você, em um tópico do Discord                 Claude Code, na janela do tmux desse tópico
        │                                                    │
        │  sua mensagem é digitada no painel  ──────────────▶│
        │                                                    │  trabalha: lê, edita, executa ferramentas
        │                                                    ▼
        │                          ~/.claude/projects/<dir>/<session>.jsonl
        │                          (o transcript que o Claude Code escreve sozinho)
        │                                                    │
        ◀──── o c-lord lê esse arquivo e publica o que diz ──┘
```

O c-lord não lê a resposta do Claude na tela do terminal e não pede ao Claude que a publique. Ele lê o transcript que o Claude Code já mantém e o encaminha. A raspagem de tela vivia vazando pedaços da interface do terminal para o Discord; pedir ao Claude que publicasse fazia um turno inteiro sumir sempre que ele esquecia. O transcript não tem nenhum dos dois problemas, e é o único caminho que uma resposta percorre.

O painel continua sendo observado, mas não pelo texto: para perceber que um turno terminou, para transformar em botões do Discord um menu que o Claude Code mostra (`AskUserQuestion`, aprovação de plano), para aceitar os pedidos de permissão do Claude Code e para relatar um prompt que o c-lord não reconhece. O [docs/ARCHITECTURE.md](../ARCHITECTURE.md) percorre tudo isso, e o [docs/DESIGN_DECISIONS.md](../DESIGN_DECISIONS.md) registra o porquê.

### Onde fica cada coisa

```text
c_lord/
  cli.py            o comando `c-lord`: setup, start, version
  main.py           ponto de entrada standalone — lê o .env, pega os locks, inicia tudo
  setup.py          setup_bridge(): registra os cogs padrão em uma chamada
  bot.py            ClaudeDiscordBot
  tmux.py           sessões e janelas do tmux; iniciar o claude, digitar no painel
  session_dir.py    um clone git por tópico
  claude/           runner por turno sobre o painel do tmux, configurações da CLI, uso do contexto
  transcript/       leitura contínua do transcript do Claude Code e formatação para o Discord; log de subagentes
  cogs/             chat, vínculo canal↔repositório, skills, comandos de workspace, agendador,
                    gatilhos de webhook, atualização automática, espelho do transcript
  discord_ui/       lâmpada de status, botões de pergunta, linha de progresso do turno, imagens de tabela e de painel
  database/         SQLite: sessões, vínculos, tarefas agendadas, notificações, menus pendentes
  ext/api_server.py REST API
  skills/           a skill discord-read colocada em cada sessão
deploy/             a unit do systemd
scripts/            install-systemd.sh, staging.sh, ferramentas de evidência e de release
docs/               guias, e docs/specs/ — como cada recurso deve se comportar
```

### O que o c-lord é, e o que não é

- **Uma ponte, não uma IA.** O c-lord não tem prompts, ferramentas nem memória próprios. Tudo isso é do Claude Code, e o que quer que você tenha configurado para o Claude Code — `CLAUDE.md`, skills, servidores MCP, hooks — funciona em um tópico porque é o mesmo programa.
- **A CLI de verdade, não a API.** Cada tópico roda o `claude` interativo que você rodaria em um terminal. O c-lord nunca chama a API da Anthropic.
- **O tmux fica.** O c-lord não substitui o tmux; ele permite gerenciar o tmux a partir do Discord. O painel está sempre lá para você se conectar.
- **Sessões que sobrevivem às conexões.** O Claude roda no servidor. O seu notebook entrar em suspensão, o seu trem entrar em um túnel ou o bot reiniciar não o interrompem.
- **Você julga pelo Discord, então o Discord precisa mostrar o suficiente para julgar.** Progresso, resultados, arquivos e falhas vão todos para o tópico, e uma falha diz o que falhou e o que fazer em seguida, em vez de ficar em silêncio.
- **Nada a configurar no caso comum.** Os novos recursos vêm ligados por padrão, com configurações sensatas, e atualizar o pacote é tudo o que quem o consome precisa fazer.

O raciocínio por trás disso está em [docs/PHILOSOPHY.md](../PHILOSOPHY.md) (em japonês).

## Segurança

Falar com o c-lord significa executar comandos na máquina em que ele vive. Os padrões partem desse pressuposto.

- **Autorização de usuários** — `allowed_user_ids` / `CLORD_ALLOWED_ROLE` restringem quem pode invocar o Claude. **Sem nenhum dos dois configurado, só o dono do próprio aplicativo do Discord pode usar o bot** — falar com o c-lord executa comandos de shell no host, então "não configurado" não é "todo mundo" (#713). `CLORD_ALLOW_ANYONE=1` o abre para o servidor inteiro de propósito (e diz isso na inicialização). Veja [docs/specs/authorization-default.md](../specs/authorization-default.md)
- **A REST API só responde ao usuário Unix do próprio bot**, a menos que você defina `CLORD_API_SECRET` (veja [REST API](#rest-api)).
- **Nenhum shell no meio.** Todo processo que o c-lord inicia é iniciado com uma lista de argumentos (`asyncio.create_subprocess_exec`), nunca por meio de um shell, e um prompt é passado depois de um `--`, para que não possa ser lido como uma flag.
- **Verificado antes do uso.** Um ID de sessão precisa ter cara de ID de sessão antes de chegar ao `--resume`; nomes de skills e URLs de repositório são validados da mesma forma.
- **O token do bot não vaza para as sessões por herança.** Ele é removido do ambiente do tmux e do ambiente da linha de comando do `claude`. Isso evita acidentes, não uma sessão determinada: o `.env` é um arquivo que o mesmo usuário Unix consegue ler.

O [docs/SECURITY.md](../SECURITY.md) traz o modelo de ameaças e os riscos aceitos de propósito.

## Desenvolvimento

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup                                # uma vez por clone: ativa o hook de pre-commit

uv run pytest tests/ -v --cov=c_lord
uv run ruff check c_lord/ && uv run ruff format --check c_lord/
uv run pyright c_lord/
```

A CI executa essas verificações em Python 3.10, 3.11 e 3.12. Elas usam mocks, então uma CI verde não mostra que uma mudança funciona com um Discord real e um tmux real — e é por isso que uma mudança também precisa ser demonstrada funcionando em um bot de staging antes do merge. O [CONTRIBUTING.md](../../CONTRIBUTING.md) explica o que um pull request precisa ter; o [docs/STAGING.md](../STAGING.md) explica os bots de staging.

## Como este projeto é construído

**Todo este código base foi escrito pelo [Claude Code](https://docs.anthropic.com/en/docs/claude-code)**, o agente de codificação com IA da Anthropic. O autor humano ([@ebibibi](https://github.com/ebibibi)) forneceu requisitos e direção em linguagem natural, mas não leu nem editou manualmente o código fonte.

O parágrafo acima descreve o projeto upstream, o claude-code-discord-bridge, que deu ao c-lord a sua arquitetura inicial sob a Licença MIT. Este derivado é mantido hoje por [@yousan](https://github.com/yousan), que define os requisitos, revisa os pull requests e aprova todas as mudanças, enquanto o Claude Code faz a implementação.

**Obrigado, [@ebibibi](https://github.com/ebibibi).** O c-lord começou a partir do código do [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge), publicado por Masahiko Ebisuda sob a licença MIT. O aviso de copyright e o texto da licença originais estão preservados em [NOTICE](../../NOTICE).

No dia a dia, funciona assim. Uma mudança começa como uma Issue com critérios de aceitação. O Claude Code — geralmente rodando em um tópico do c-lord, de modo que o c-lord é construído com o c-lord — escreve um teste que falha, faz o teste passar, mostra a mudança funcionando em um bot de staging com capturas de tela e abre um pull request. Duas verificações obrigatórias protegem a `main`, e um pull request só recebe merge quando todos os itens da [Definition of Done](../../CLAUDE.md#definition-of-done-dod--single-source-of-truth) são atendidos.

A história própria do c-lord começa em 2026-02-22, com uma cópia desse projeto upstream. Relatos de bugs e pull requests são bem-vindos, tanto de pessoas quanto de agentes.

## Exemplo Real

**[EbiBot](https://github.com/ebibibi/discord-bot)** — Um bot pessoal do Discord construído sobre este framework. Inclui sincronização automática de documentação (inglês + japonês), notificações push, watchdog do Todoist, verificações de saúde agendadas e CI/CD com GitHub Actions. Use-o como referência para construir seu próprio bot.

O EbiBot é o bot do próprio ebibibi, construído sobre o projeto upstream claude-code-discord-bridge — não sobre o c-lord.

## Inspirado em

- [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge), de [@ebibibi](https://github.com/ebibibi) — A base upstream sobre a qual este projeto foi construído.
- [OpenClaw](https://github.com/openclaw/openclaw) — Reações emoji de status, debounce de mensagens, divisão com conhecimento de blocos
- [claude-code-discord-bot](https://github.com/timoconnellaus/claude-code-discord-bot) — Abordagem de invocação CLI + stream-json
- [claude-code-discord](https://github.com/zebbern/claude-code-discord) — Padrões de controle de permissões
- [claude-sandbox-bot](https://github.com/RhysSullivan/claude-sandbox-bot) — Modelo de thread por conversa

## Licença

[MIT](../../LICENSE). O aviso de copyright e a licença do código de onde o c-lord partiu estão em [NOTICE](../../NOTICE).
