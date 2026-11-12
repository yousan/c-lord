# c-lord

[![CI](https://github.com/yousan/c-lord/actions/workflows/ci.yml/badge.svg)](https://github.com/yousan/c-lord/actions/workflows/ci.yml)
[![Python 3.10 – 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg)](../../pyproject.toml)
[![MIT license](https://img.shields.io/badge/license-MIT-green.svg)](../../LICENSE)

**Ejecute varias sesiones de Claude Code en paralelo desde Discord: cada hilo trabaja en su propio clon de git, así que las sesiones nunca comparten un árbol de trabajo.**

c-lord convierte un servidor de Discord en la sala de control de las sesiones de Claude Code que se ejecutan en su propia máquina. Asigne un hilo a cada tarea, váyase y consúltelas todas desde el teléfono, el portátil o el terminal: nada se interrumpe y todo lo ocurrido queda en el hilo.

<p align="center">
  <img src="../assets/demo-mock.svg" alt="Maqueta: un canal de Discord con cuatro hilos de Claude Code, uno de ellos abierto con una petición, un mensaje escrito en el terminal, la respuesta con archivos y un terminal conectado a la misma sesión (todos los nombres son ficticios)" width="760">
</p>

## Por qué c-lord

- 🎛️ **Discord se convierte en la sala de control de muchas sesiones de Claude Code.** Un hilo es un Claude Code real e interactivo que se ejecuta en tmux en su máquina, el mismo que usaría en un terminal. Suelte archivos en el hilo y Claude los recibe; los archivos que crea Claude vuelven como adjuntos. ¿Quiere el teclado? Haga `tmux attach` a esa misma sesión.
- ⏳ **Sin límite de tiempo.** Un trabajo largo nunca se detiene por tardar mucho. c-lord solo da por perdido un turno cuando la pantalla de Claude lleva 5 minutos congelada, y ni siquiera entonces mata a Claude.
- 🧪 **Un espacio de trabajo completo y separado por hilo.** Cada hilo trabaja en su propio clon del repositorio, así que puede ejecutar a la vez su backend y su frontend, y su propio stack de `docker compose`, sin tocar los de otro hilo. c-lord sabe qué contenedores pertenecen a cada hilo y los detiene con él. (Se activa vinculando el canal a un repositorio con `/clord-init`; los puertos del host siguen siendo compartidos, así que asigne los suyos a cada stack.)
- 📜 **Todo se conserva en Discord.** Las respuestas, la actividad de las herramientas, los archivos e incluso lo que usted escribe directamente en el panel de tmux acaban en el hilo. Su equipo puede seguir el trabajo, y otro agente de IA puede leer el historial para retomarlo.
- 🔒 **Usted decide quién puede manejarlo.** De fábrica solo puede usarlo el propietario de la aplicación de Discord (hablar con c-lord ejecuta comandos en su máquina). Ábralo a un rol o a una lista de personas cuando quiera.

**Para empezar:** [Inicio rápido](#inicio-rápido) · [Cómo funciona](#la-idea-central-sesiones-en-paralelo-cada-una-en-su-propio-clon) · [Comandos](../COMMANDS.md) · [Configuración](#configuración) · [Arquitectura](#arquitectura)

[English](../../README.md) · [日本語](../ja/README.md) · [简体中文](../zh-CN/README.md) · [한국어](../ko/README.md) · Español · [Português](../pt-BR/README.md) · [Français](../fr/README.md)

Esta página traduce el [README en inglés](../../README.md); si los dos textos no coinciden, el correcto es el inglés.

> **Basado en [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge), de [@ebibibi](https://github.com/ebibibi) (Masahiko Ebisuda).** Este proyecto fue creado originalmente por ebibibi y se usa aquí bajo la licencia MIT. Gracias por esa excelente base.

> c-lord es un proyecto independiente de código abierto. Anthropic no lo desarrolla, patrocina ni avala; «Claude» y «Claude Code» son marcas comerciales de Anthropic. c-lord se limita a manejar la CLI de Claude Code que usted mismo instaló y en la que usted mismo inició sesión.

**En esta página:** [Cómo funciona](#la-idea-central-sesiones-en-paralelo-cada-una-en-su-propio-clon) · [Inicio rápido](#inicio-rápido) · [Trabajar en un hilo](#trabajar-en-un-hilo) · [Espacios de trabajo](#espacios-de-trabajo) · [Comandos](#comandos) · [Configuración](#configuración) · [Tareas programadas](#tareas-programadas) · [GitHub y CI](#automatización-con-github-y-ci) · [API REST](#api-rest) · [Como biblioteca](#usar-c-lord-como-biblioteca) · [Arquitectura](#arquitectura) · [Seguridad](#seguridad) · [Desarrollo](#desarrollo) · [Cómo se construye](#cómo-se-construye-este-proyecto)

## La idea central: sesiones en paralelo, cada una en su propio clon

Cada hilo de Discord es su propia sesión de Claude Code, que se ejecuta en su propia ventana de tmux en su servidor. Envíe tareas a varios hilos y se ejecutarán a la vez; cierre el portátil y seguirán adelante; responda en un hilo para continuar donde lo dejó.

Lo que impide que las sesiones en paralelo se pisen entre sí es el **aislamiento**: cuando un canal está vinculado a un repositorio (`/clord-init`), cada hilo trabaja en su propio `git clone` independiente. Las sesiones nunca comparten un árbol de trabajo, de modo que los cambios sin confirmar de un hilo no pueden aparecer en otro. Solo se encuentran a través del remoto de git —ramas, pushes y PR—, igual que los colaboradores humanos.

```text
Hilo A (feature)        ──→  Claude Code  (clon propio)  ─┐
Hilo B (revisión de PR) ──→  Claude Code  (clon propio)   ├─→  remoto de git (ramas · PR)
Hilo C (docs)           ──→  Claude Code  (clon propio)  ─┘
```

**Hoy las sesiones no se coordinan entre sí.** Versiones anteriores de este README describían cuatro mecanismos de coordinación; este es su estado real ([#758](https://github.com/yousan/c-lord/issues/758)):

| Mecanismo | Estado real |
|---|---|
| **Aviso de concurrencia** (instrucciones a Claude sobre el trabajo en paralelo) | **No implementado**: las sesiones se ejecutan en la TUI de tmux (#53), que no tiene un canal para el prompt de sistema de cada turno, así que nunca llegó a entregarse; c-lord dejó de generarlo (#766) |
| **Registro de sesiones activas** | Existe en memoria y lo usa `/workspace-cleanup`, pero **las sesiones no lo ven** (nada se lo envía) |
| **Canal de coordinación** | **Desactivado por defecto.** Con `COORDINATION_CHANNEL_ID` definido, publica un aviso de una línea cuando termina el turno de una sesión; no hay evento de inicio |
| **AI Lounge** | Los endpoints REST funcionan, pero **nada le dice a Claude que el lounge existe**, así que en la práctica no se publica nada; véase [AI Lounge](#ai-lounge-no-se-entrega-a-las-sesiones) |

En resumen: árboles de trabajo separados, no coordinación en vivo. Todo lo que queda fuera de los clones —la misma rama remota, un puerto o una base de datos compartidos, un bot de staging— puede seguir chocando. Dé a cada hilo su propia rama.

### AI Lounge (no se entrega a las sesiones)

> **Estado: sin conectar** ([#758](https://github.com/yousan/c-lord/issues/758)). El lounge se diseñó para inyectarse en cada sesión como contexto de sistema efímero. Como las sesiones se ejecutan en la TUI de tmux (#53), no hay un canal por turno para eso, así que c-lord no lo genera (#766): a Claude nunca se le dice que el lounge existe y, en la práctica, no se publica nada.

Los endpoints REST siguen funcionando, así que puede usar el lounge por su cuenta; por ejemplo, describiéndolo en el `CLAUDE.md` de su repositorio:

```bash
# Publicar una nota (se guarda en SQLite; también se reenvía al canal del lounge, que por defecto es COORDINATION_CHANNEL_ID)
curl -X POST "http://localhost:8080/api/lounge" \
  -H "Content-Type: application/json" \
  -d '{"message": "Starting auth refactor on feature/oauth", "label": "feature dev"}'

# Leer las notas recientes
curl "http://localhost:8080/api/lounge"
```

## Inicio rápido

Necesita una máquina Linux (WSL2 sirve) que permanezca encendida, con esto instalado:

- Python 3.10 o posterior y [uv](https://docs.astral.sh/uv/)
- `git` y `tmux`
- la [CLI de Claude Code](https://docs.anthropic.com/en/docs/claude-code), con la sesión ya iniciada; ejecute `claude` una vez a mano para comprobarlo
- un servidor de Discord al que se le permita añadir un bot

### Paso 1 — Crear el bot de Discord

1. Abra el [Discord Developer Portal](https://discord.com/developers/applications) y cree una **New Application**.
2. En su página **Bot**, active **Message Content Intent** (en *Privileged Gateway Intents*) y copie el **token** del bot.
3. En **OAuth2 → URL Generator**, marque los scopes `bot` y `applications.commands`, y después estos permisos del bot:

   | Permiso | Para qué se usa |
   |---|---|
   | View Channel (Ver canales) | ver el canal en sí (véase la nota de abajo) |
   | Send Messages (Enviar mensajes), Send Messages in Threads (Enviar mensajes en hilos) | las respuestas |
   | Create Public Threads (Crear hilos públicos) | un hilo por sesión |
   | Read Message History (Leer el historial de mensajes) | ponerse al día con los mensajes publicados mientras el bot no estaba |
   | Add Reactions (Añadir reacciones), Manage Messages (Gestionar mensajes) | el indicador de estado sobre su mensaje, y quitarlo |
   | Manage Webhooks (Gestionar webhooks) *(opcional)* | mensajes de los subagentes con su propio nombre (`CLORD_SUBAGENT_VOICES=1`) |

4. Abra la URL generada y añada el bot a su servidor.

> **Canales privados o con permisos restringidos:** si un canal deniega **View Channel**
> a `@everyone` (un canal privado), la invitación a nivel de servidor de arriba *no* basta:
> la denegación a nivel de canal de Discord tiene prioridad. Debe añadir el **rol del bot**
> (o el bot como miembro) a los permisos específicos de ese canal y concederle al menos
> **View Channel**, **Create Public Threads**, **Send Messages** y **Send
> Messages in Threads**. Conceder solo «Manage Threads» **no** sirve.
>
> Cuando al bot le faltan estos permisos en un canal, **`/clord` responde (de forma efímera)
> con los permisos exactos que hay que conceder** (#443): un comando de barra llega al bot
> tanto si puede ver el canal como si no. Corrija los permisos antes de continuar:
> sin **View Channel** el bot no recibe ningún mensaje de ese canal, así que
> lo que usted escribiera en un hilo de allí nunca llegaría a Claude.

### Paso 2 — Ejecutar el asistente de configuración

El asistente escribe el `.env` por usted; no hay nada que editar a mano.

```bash
# Directamente desde GitHub, sin instalar nada:
uvx --from "git+https://github.com/yousan/c-lord.git" c-lord setup

# O desde un clon (que es también lo que conviene para ejecutarlo como servicio):
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv run c-lord setup
```

Pregunta seis cosas, en este orden:

1. **CLI de Claude Code**: solo comprueba que `claude` se puede ejecutar, y le avisa si no es así.
2. **Token del bot**: el token se comprueba contra la API de Discord (un token pegado como `Bot xxx` se guarda como `xxx`; tras 3 respuestas vacías o no válidas, se detiene sin escribir el `.env`).
3. **Canal**: muestra los canales de texto que el bot puede ver; elija uno por su número (sin copiar ID) o pegue un ID de canal numérico (si recibe la URL de un canal, o dígitos como `²`, vuelve a preguntar). Este pasa a ser `DISCORD_CHANNEL_ID`, el canal principal del bot.
4. **Su ID de usuario de Discord**: opcional; es a quien se @menciona. Si recibe un nombre de usuario en lugar del ID numérico vuelve a preguntar, porque `c-lord start` lo rechaza.
5. **Directorio de trabajo**: el directorio por defecto para las ejecuciones que no tienen un clon propio.
6. **Modelo**: `sonnet`, `opus` o `haiku`.

Después escribe el `.env` (use `--env PATH` para ponerlo en otro sitio) y le ofrece arrancar el bot en ese mismo momento.

### Iniciar / detener

```bash
c-lord start    # inicia el bot (lee el .env del directorio actual)
c-lord start --env /path/to/.env   # .env en otra ubicación
c-lord version  # muestra el build en ejecución, p. ej. v1.4.183-bd80c47e-20260908
```

En un clon, anteponga `uv run` a estos comandos. Un bot iniciado así se ejecuta en primer plano; Ctrl-C lo detiene. Para mantenerlo en marcha, véase [Ejecutar como servicio](#ejecutar-como-servicio-systemd---user--sobrevive-a-los-reinicios).

> **Precedencia de la configuración (#324):** las claves definidas en el archivo `.env` siempre
> prevalecen sobre las variables de entorno heredadas del proceso: el directorio desde el que
> arranca determina la identidad del bot. Las claves *ausentes* del archivo siguen recurriendo
> al entorno del proceso, así que las instalaciones que solo usan variables de entorno siguen funcionando.

### Paso 3 — Vincular un repositorio e iniciar una sesión

Las sesiones se inician con un comando de barra, nunca con un mensaje normal escrito en el propio canal (esos se ignoran, de modo que el canal puede seguir siendo un lugar normal para conversar).

```text
/clord-init repo:https://github.com/you/your-project.git     ← una vez por canal
/clord Fix the flaky login test                               ← abre un hilo e inicia Claude en él
```

`/clord-init` registra en qué repositorio trabaja el canal. De forma predeterminada, Discord solo lo ofrece a los miembros con el permiso **Manage Server** (Gestionar servidor). A partir de ahí, cada `/clord` abre un hilo nuevo, clona ese repositorio para él e inicia Claude Code en el clon. Todo lo que usted escriba **dentro de ese hilo** va a la misma sesión de Claude. Para usar otro repositorio en un hilo concreto, indíquelo: `/clord repo:<url> <prompt>`; esto también funciona en un canal sin vinculación.

El host clona con sus propias credenciales de git, así que un repositorio privado necesita que allí haya configurada una clave SSH o un asistente de credenciales. Cuando un clon falla, el hilo muestra el error del propio git y qué comprobar ([especificación](../specs/workspace-setup-failure.md)).

### Ejecutar como servicio (systemd --user — sobrevive a los reinicios)

Para mantener c-lord en ejecución en segundo plano y **arrancarlo automáticamente cuando se reinicia el host**, instálelo como servicio `systemd --user`. Desde su clon:

```bash
bash scripts/install-systemd.sh
```

Esto instala [`deploy/c-lord.service`](../../deploy/c-lord.service) tal cual como `~/.config/systemd/user/c-lord.service`, escribe los valores propios del host (la ruta de este clon, su `uv`, su `PATH`) en el drop-in `c-lord.service.d/10-install.conf`, habilita el servicio y activa **linger** para que arranque con el sistema aunque nadie haya iniciado sesión. Vuelva a ejecutarlo cuando cambie `deploy/c-lord.service`. Después, gestiónelo con el systemd habitual:

```bash
systemctl --user restart c-lord.service     # después de un `git pull`
systemctl --user stop    c-lord.service
systemctl --user status  c-lord.service
journalctl --user -u c-lord.service -f      # registros en vivo
```

Una vez instalada la unidad, **systemd es lo único que arranca el bot de este clon.** No lo mate para arrancarlo a mano: systemd intentaría relanzarlo, chocaría con el bloqueo de instancia única, se rendiría (`Start request repeated too quickly`) y dejaría el bot ejecutándose sin supervisión. `bash scripts/staging.sh restart|stop` lo sabe: en un clon al que apunta el `WorkingDirectory=` de una unidad de usuario, llama a `systemctl --user restart|stop` en lugar de matar el proceso, y termina con error (sin matar nada) cuando `systemctl --user` no está disponible. `bash scripts/staging.sh status` muestra si cada proceso del bot está dentro del cgroup de la unidad.

Por qué `--user` y no un servicio de sistema: c-lord maneja el `tmux` del usuario, los archivos de sesión de `~/.claude`, `uv` y la CLI `claude`, así que como mejor funciona es ejecutándose como **usted**, con su entorno; un servicio de root o de sistema tendría que recrear todo eso mediante `User=`/`Environment=`. `loginctl enable-linger "$USER"` es lo que hace que un servicio de usuario sobreviva al cierre de sesión y arranque con el sistema; el instalador lo ejecuta por usted.

> **Instalación manual:** copie [`deploy/c-lord.service`](../../deploy/c-lord.service), corrija
> las dos rutas `CHANGE-ME` (directorio del clon y `uv`) y después ejecute
> `systemctl --user daemon-reload && systemctl --user enable --now c-lord.service && loginctl enable-linger "$USER"`.

> **Nota para WSL:** si `systemctl --user` responde `Failed to connect to bus`, la sesión
> D-Bus del usuario no está activa. Asegúrese de tener `systemd=true` bajo `[boot]` en
> `/etc/wsl.conf` y reinicie WSL (`wsl --shutdown`), o bien reinicie el bot
> matándolo y dejando que `Restart=always` lo relance (`pkill -f c_lord.main` no es
> seguro con varios clones: mate el proceso por su PID).

## Trabajar en un hilo

Lo que sigue es lo que usted ve realmente en Discord. Cada punto enlaza a la página que define cómo debe comportarse; el índice completo está en [docs/specs/](../specs/README.md) y la guía para el día a día es [docs/USER_GUIDE.md](../USER_GUIDE.md).

### Hablar con Claude

- **Un hilo, una conversación.** Una respuesta en el hilo continúa la misma sesión de Claude Code (`--resume`), con su contexto intacto: hoy, mañana o después de que el bot se haya reiniciado.
- **Un mensaje nuevo interrumpe.** Si Claude sigue trabajando cuando usted vuelve a escribir, el turno en curso se interrumpe y lo que se ejecuta es su nueva instrucción. No hace falta un `/stop` antes.
- **Detener.** `/stop`, o el botón **⏹ Stop** que aparece al final del hilo mientras se ejecuta un turno, detiene el turno y los comandos de shell que Claude dejó en ejecución. La conversación se conserva; su siguiente mensaje la continúa ([especificación](../specs/stop-button.md)).
- **Sin límite de longitud en lo que envía.** Los textos largos pegados se entregan enteros. Si de verdad un mensaje no se puede entregar, el hilo lo dice: nunca termina un turno vacío en silencio ([especificación](../specs/input-delivery.md)).
- **Archivos de entrada.** Adjunte hasta 10 archivos a un mensaje. Cada uno se guarda dentro del clon del hilo (`.clord/attachments/…`, fuera de git) y a Claude se le indica la ruta ([especificación](../specs/attachments.md)).
- **Archivos de salida.** Un archivo que Claude entrega se adjunta al hilo con su nombre real, hasta 25 MB por defecto ([especificación](../specs/user-file-delivery.md)).
- **Las respuestas largas llegan completas.** Una respuesta que supera el límite de 2000 caracteres de Discord se divide en varios mensajes sin romper los bloques de código ([especificación](../specs/long-reply-split.md)).
- **El terminal sigue ahí.** Haga `tmux attach` a la ventana del hilo y escriba; lo que escriba allí aparece en el hilo como suyo, y la respuesta de Claude llega como siempre ([especificación](../specs/mirrored-events.md)).
- **Los mensajes publicados mientras el bot estaba caído no se pierden.** Cuando se reconecta o se reinicia, lee lo que llegó entretanto a sus hilos y lo ejecuta, con una línea que explica el retraso ([especificación](../specs/gateway-backfill.md)).
- **Los nombres de los hilos son suyos**: c-lord nunca vuelve a resumir el nombre de un hilo por su cuenta. Un hilo conserva el nombre con el que se abrió (solo se le añaden el prefijo `W<N> │` y el número `#<issue>`); ejecute `/thread-rename` en el hilo cuando quiera un resumen nuevo de la conversación reciente (sonnet). `CLORD_AUTO_TOPIC=1` recupera el antiguo resumen automático (#705)

### Saber qué está pasando

- **Estado en tiempo real** — Un indicador en forma de reacción emoji sobre su mensaje: 🟢 en ejecución mientras Claude trabaja, 🟡 en espera cuando le toca a usted (❌ si hay un error, ⏳ / ⚠️ si se queda en silencio durante 10 s / 30 s). Las reacciones siguen respondiendo con rapidez bajo un uso intenso. El indicador 🟢/🟡 en el nombre del hilo está desactivado por defecto (#329); defina `CLORD_THREAD_LAMP=1` para tener esa vista en la barra lateral, más lenta y de consistencia eventual (#246)
- **Actividad de las herramientas** — Se lee de la transcripción del propio Claude Code, no se publica herramienta a herramienta: un turno que se queda en silencio muestra una línea de progreso (`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`, es decir, «trabajando 5:56 · … · 61 herramientas»; véase *Línea de progreso del turno* más abajo), y las llamadas a herramientas y las frases de progreso del turno se adjuntan a la respuesta como `progress.txt` (#723)
- **Línea de progreso del turno** — Cuando un turno lleva 90 s en silencio, aparece una línea de subtexto (`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`), que se actualiza en el mismo sitio cada 15 s y desaparece en cuanto vuelve la salida real; dice `⏳ 待機中` («en espera») cuando incluso la actividad de las herramientas se ha detenido. Cuando Claude escribe una frase de progreso (la línea «●» del panel), la línea muestra la más reciente en lugar de la herramienta (#883); esas frases también acaban en `progress.txt`, nunca como mensajes propios. Nunca se publica fuera de un turno. Se desactiva con `CLORD_TURN_PROGRESS=0` (#539)
- **Uso del contexto** — Cada turno termina con un pie de una línea (`📊 7% context (69k/1.0M) · …`): porcentaje de la ventana de contexto (tokens de entrada + caché, sin contar la salida), modelo, versiones y coste; por encima del 83,5 %, cuando la compactación automática puede ejecutarse en el turno siguiente, se convierte en una advertencia ⚠️ (`docs/specs/context-footer.md`)
- **Aviso de compactación** — Cuando Claude Code compacta el contexto, el hilo recibe una línea `🗜️ コンテキストを圧縮しました` («se ha compactado el contexto») en lugar del resumen de continuación en bruto (#628)
- **Indicador de bloqueo** — Tras 30 s sin actividad, la reacción sobre su mensaje pasa a ⚠️ (el pensamiento extendido o la compactación del contexto también pueden ser así de silenciosos); no se publica ningún mensaje en el hilo (#473)
- **Aviso de fin de turno** — Cuando el turno termina, el hilo @menciona a quien lo pidió, así que usted recibe una notificación. Un turno que no produjo ninguna respuesta lo dice, en lugar de afirmar que ha terminado ([especificación](../specs/turn-end-notification.md)).
- **Notificaciones de tiempo de espera agotado** — Un embed con el tiempo transcurrido e indicaciones para reanudar, que solo se emite cuando Claude está realmente atascado (el panel congelado durante toda la ventana de tiempo *y* sin estar inactivo en su prompt); un turno que terminó con normalidad nunca lo provoca (#541), y tampoco un menú de pregunta o de plan que sigue esperando su respuesta (#751)
- **Límite de uso e inicio de sesión** — Alcanzar el límite de uso del plan se comunica exactamente como tal, con la hora a la que se restablece ([especificación](../specs/usage-limit.md)). Cuando caduca el inicio de sesión de Claude Code, el propietario recibe un único MD que indica el host y los hilos que se detuvieron, y puede volver a iniciar sesión desde ese MD ([especificación](../specs/login-alert.md)).
- **Panel 📊 Session Status** — Desactivado por defecto (#761). Defina `CLORD_SESSION_STATUS_BOARD=1` para mantener en `DISCORD_CHANNEL_ID` un panel en vivo que muestra cada sesión como 🟢 Auto-processing / 🟡 Waiting for input. Mientras está desactivado, un bot que arranca no publica nada en el canal y retira cualquier panel que hubieran dejado arranques anteriores; el aviso de fin de turno `🟡 Claude has finished … @you` en el hilo no se ve afectado. Véase `docs/specs/session-status-board.md`

### Cuando Claude le pregunta algo

- **Preguntas** — Cuando Claude llama a `AskUserQuestion`, las opciones aparecen en el hilo como botones o como un menú de selección, y Claude continúa con lo que usted eligió. Los botones siguen funcionando después de reiniciar el bot ([especificación](../specs/ask-menu-lifecycle.md)).
- **Modo Plan** — Cuando Claude llama a `ExitPlanMode`, el menú de aprobación que muestra Claude Code se lleva a Discord igual que `AskUserQuestion`: las opciones aparecen como botones y Claude sigue con la que usted elija (#251)
- **Solicitudes de permiso para herramientas** — No se muestran en Discord: c-lord acepta la solicitud de permiso de Claude Code en el panel para que el turno continúe. Lo que Claude puede hacer lo deciden el modo de permisos y los ajustes con los que se ejecuta la sesión, no un botón por herramienta

### Dividir el trabajo

- **Registro de subagentes** — El trabajo repartido entre subagentes dentro de un hilo queda en Discord: una línea `🤖 サブエージェント「…」開始` («subagente “…” iniciado») / `終了 · 6:12 · ツール 48 件` («finalizado · 6:12 · 48 herramientas») por ejecución; la línea de fin lleva `subagent-*.md` (informe completo, encargo, llamadas a herramientas) y la copia de la línea de inicio se actualiza mientras se ejecuta. `CLORD_SUBAGENT_VOICES=1` añade los mensajes y el informe de cada agente con su propio nombre (#971, `docs/specs/subagent-log.md`)

**Cuándo abrir un hilo nuevo y cuándo dividir el trabajo dentro de uno** (#969): abra un hilo nuevo solo cuando el trabajo necesite su propia rama, su propio servidor de desarrollo o su propia conversación con usted. Para dividir un mismo trabajo —investigación, revisiones, clasificación— pida a Claude que use subagentes dentro del hilo: no se inicia ningún proceso `claude` adicional (≈285 MB cada uno), y el inicio, el fin y el informe completo de cada subagente siguen llegando al hilo ([registro de subagentes](../specs/subagent-log.md)). Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`) es experimental y sus compañeros de equipo no vuelven cuando c-lord se reinicia o reanuda la sesión; no use su vista de paneles divididos (`--teammate-mode tmux`), que añade paneles a la ventana que maneja c-lord.

### Skills, modelos y commits

- **Skills** — `/skill` ejecuta una de las skills de Claude Code que hay en `~/.claude/skills/`. El nombre se autocompleta, y una skill añadida mientras el bot está en marcha aparece en menos de un minuto. En un canal abre un hilo nuevo; en un hilo continúa esa sesión.
- **Cambio de modelo en tiempo de ejecución** — `/model` muestra y cambia el modelo y el esfuerzo a nivel de sesión, de canal o global (`include-existing` cambia también las sesiones abiertas a partir de su siguiente turno)
- **Quién pidió un commit** — Los commits que Claude hace en un hilo llevan trailers `Co-authored-by:` del usuario de Discord que pidió ese turno y de Claude. Lo hace un hook de git, no una petición a Claude para que se acuerde; `CLORD_COAUTHOR=0` lo desactiva ([especificación](../specs/commit-coauthors.md)).

## Espacios de trabajo

Un **espacio de trabajo** (workspace) es todo lo que pertenece a un hilo: su sesión de Claude, su clon y cualquier entorno de docker iniciado desde ese clon.

- **Un clon de git por hilo** — Con un canal vinculado a un repositorio (`/clord-init`), cada hilo trabaja en su propio `git clone` independiente; las sesiones no comparten nada salvo el remoto de git
- **Tres niveles de «apagado», cada uno incluye el anterior.** Ninguna de estas operaciones elimina el historial de la conversación ni un volumen de docker ([vocabulario](../specs/workspace-vocabulary.md)):

  | | Qué se detiene | Por sí solo | A mano |
  |---|---|---|---|
  | **Suspensión** (sleep) | el proceso `claude` (docker sigue en ejecución) | tras 4 horas de inactividad (`CLORD_IDLE_SLEEP_HOURS`) | — el siguiente mensaje lo despierta sin decir nada |
  | **Detención** (stop) | + el entorno de docker | tras 7 días de inactividad (`CLORD_IDLE_STOP_DAYS`) | `/workspace-stop`, se deshace con `/workspace-start` |
  | **Eliminación** (delete) | + el propio clon | tras 30 días sin uso, véase más abajo | `/workspace-delete` |

- **Un tope de cuántos permanecen despiertos.** Por encima de `CLORD_MAX_RESIDENT_WORKSPACES` (calculado por defecto a partir de la memoria del host), se suspenden los espacios de trabajo que llevan más tiempo inactivos. Iniciar uno nuevo nunca tiene que esperar ([especificación](../specs/resident-cap.md)).
- **Estado del canal** — `/clord-status` muestra las sesiones de este canal con el tamaño del directorio, el destino de `tmux attach` y el comando `claude --resume`; `show_all` incluye las sesiones cerradas (al estilo de `docker ps -a`). Sustituye a los antiguos `/sessions`, `/session-dirs` y `/resume-info`.
- **Limpieza de espacios de trabajo** — `/workspace-cleanup` recupera los directorios de clones que ninguna sesión en ejecución está usando (`dry_run` para previsualizar); un directorio que contiene trabajo (cambios sin confirmar, commits sin enviar, archivos ignorados como `.env`) nunca se elimina, y la respuesta dice cuáles (#1016)
- **Limpieza a los 30 días, con aviso** — En cada arranque se limpian los espacios de trabajo que llevan **30 días** sin usarse. El plazo sigue la retención de transcripciones del propio Claude Code (`cleanupPeriodDays`), así que para entonces Claude normalmente ya ha olvidado la conversación. El clon solo se elimina si no contiene trabajo sin confirmar; el registro que vincula el hilo con su sesión se conserva. Un hilo que sigue abierto recibe un aviso 🧹 que dice qué ocurrió y qué se conservó, y después se archiva; un hilo ya cerrado se limpia sin publicar nada, para que siga cerrado. Si más adelante se escribe en un hilo así, se reconecta con lo que quede en disco o se inicia una conversación nueva en el mismo hilo. Detalles: [Por qué un hilo pierde su registro](../COMMANDS.md#why-a-thread-loses-its-record-the-30-day-sweep).
- **Los clones comparten objetos de git.** Los clones de un mismo repositorio toman prestado de un único mirror local, así que el clon de un hilo nuevo tarda segundos y ocupa poco disco. No cambia nada en su forma de usar git; `CLORD_GIT_MIRRORS=0` lo desactiva ([especificación](../specs/git-object-mirrors.md)).

## Comandos

Todos los comandos se describen en **[docs/COMMANDS.md](../COMMANDS.md)**. Los que más va a usar:

| Comando | Dónde | Qué hace |
|---|---|---|
| `/clord-init repo:<url>` | canal | Vincula el canal a un repositorio (sin argumento: muestra las vinculaciones) |
| `/clord <prompt>` | canal | Abre un hilo e inicia una sesión; `repo:<url>` elige un repositorio para ese hilo |
| `/stop` | hilo | Detiene el turno en curso; la conversación se conserva |
| `/clear` · `/compact` | hilo | Empezar de nuevo en el mismo hilo · resumir el contexto para liberar la ventana |
| `/skill <name> [args]` | canal o hilo | Ejecuta una skill de Claude Code |
| `/model` | cualquier lugar | Muestra o cambia el modelo y el esfuerzo de la sesión, del canal o de todo |
| `/clord-status` | cualquier lugar | Las sesiones de este canal, con el destino de `tmux attach` y el comando `claude --resume` |
| `/workspace-stop` · `/workspace-start` · `/workspace-delete` · `/workspace-cleanup` | hilo / cualquier lugar | Las operaciones sobre los [espacios de trabajo](#espacios-de-trabajo) |
| `/claude-restart` · `/resync` · `/tmux-screenshot` | hilo | Reiniciar el proceso de Claude · reconectar el hilo con su panel · publicar una imagen del panel |
| `/thread-rename` | hilo | Vuelve a resumir el nombre del hilo |
| `/clord-reattach` | hilo | Reconecta un hilo ya limpiado con lo que queda en disco |
| `/version` | cualquier lugar | Qué build se está ejecutando |

La mayoría de los comandos de barra existen también como comando de texto con `!` delante (`!clord`, `!stop`, …). Esos son los que puede enviar un webhook —Discord no permite que los webhooks usen comandos de barra— y son la forma en que los scripts y otros agentes inician trabajo: un mensaje de webhook que empieza por `!clord`, publicado en un canal vinculado a un repositorio, abre una sesión.

## Configuración

Todo se configura en el `.env`. Solo las dos primeras variables son obligatorias. [.env.example](../../.env.example) es un punto de partida comentado, no la lista completa: algunas de las variables de las tablas siguientes no están en él, y contiene unas pocas (para las herramientas de pruebas y de monitorización) que no se recogen aquí.

**Conexión**

| Variable | Qué hace | Valor por defecto |
|---|---|---|
| `DISCORD_BOT_TOKEN` | El token del bot | (obligatorio) |
| `DISCORD_CHANNEL_ID` | ID del canal para el chat con Claude. Como todos los ajustes de ID de aquí (`DISCORD_OWNER_ID`, `COORDINATION_CHANNEL_ID`, `EXPECTED_BOT_USER_ID`), solo admite dígitos ASCII: un enlace, un nombre o `²` detienen `c-lord start` indicando el nombre y el valor del ajuste (#1084); los espacios alrededor de los dígitos se ignoran (#1194) | (obligatorio) |
| `EXPECTED_BOT_USER_ID` | El ID de usuario del propio bot. Si el proceso inicia sesión como otro bot, termina de inmediato; conviene definirlo cuando hay varios bots en un mismo host | (desactivado) |

**Claude Code**

| Variable | Qué hace | Valor por defecto |
|---|---|---|
| `CLAUDE_COMMAND` | El ejecutable de Claude Code que se lanza | `claude` |
| `CLAUDE_MODEL` | Modelo para las sesiones nuevas. `/model` lo cambia en tiempo de ejecución | `sonnet` |
| `CLAUDE_EFFORT` | Esfuerzo de razonamiento que se pasa como `--effort` (`low` / `medium` / `high` / `xhigh` / `max`). Si no se define, no se pasa ningún flag | (el de la CLI) |
| `CLAUDE_PERMISSION_MODE` | Modo de permisos con el que se ejecutan las sesiones | `acceptEdits` |
| `CLAUDE_WORKING_DIR` | Directorio para las ejecuciones que no traen un clon propio (una tarea programada o un disparador de webhook sin `working_dir`) | directorio actual |
| `CLORD_SESSION_PLUGINS` | `0` hace que c-lord deje de excluir el plugin de Discord y los plugins de servidores de lenguaje innecesarios al iniciar una sesión ([especificación](../specs/session-plugins.md)) | `true` |

**Quién puede usarlo**

| Variable | Qué hace | Valor por defecto |
|---|---|---|
| `DISCORD_OWNER_ID` | ID del usuario autorizado a manejar el bot, y al que se @menciona cuando Claude necesita una respuesta. Si no se define, el bot solo admite al propietario de la aplicación de Discord (#713) | (opcional) |
| `CLORD_ALLOWED_ROLE` | Nombre del rol de Discord cuyos miembros pueden manejar el bot (se combina con OR con `DISCORD_OWNER_ID`) | (opcional) |
| `CLORD_ALLOW_ANYONE` | `1` permite que **todos** los miembros del servidor manejen el bot, lo que significa ejecutar comandos de shell en el host. Se advierte de ello al arrancar | `false` |
| `CLORD_NOTIFY_DENIED` | `0` suprime el MD que recibe el propietario de la aplicación cuando alguien no autorizado publica en un hilo del bot (el remitente nunca ve nada; la denegación se registra igualmente — #346) | `true` |
| `CLORD_HOST_LABEL` | Cómo llama a esta máquina el MD de caída del inicio de sesión (#1309: sirve para distinguir un segundo host) | nombre del host |
| `CLORD_LOGIN_RELAY` | `0` mantiene el MD de caída del inicio de sesión, pero quita la posibilidad de volver a iniciar sesión desde él (enlace de inicio de sesión + respuesta con el código, #1310) | `true` |

**Límites y espacios de trabajo**

| Variable | Qué hace | Valor por defecto |
|---|---|---|
| `MAX_CONCURRENT_SESSIONS` | Máximo de **turnos en ejecución a la vez**. No es un tope de procesos `claude` residentes: el semáforo solo envuelve la ejecución del turno y se libera cuando este termina, mientras que el panel de tmux sigue vivo. Para el tope de residentes, véase `CLORD_MAX_RESIDENT_WORKSPACES` (#576). Debe ser un número entero **≥ 1**: `0`, los negativos y lo que no sea un número detienen el arranque indicando la clave (#1119). Un número de más de 18 dígitos se ignora con un WARNING que indica la clave, y se usa el valor por defecto (#1256). | `3` |
| `CLORD_MAX_RESIDENT_WORKSPACES` | Máximo de espacios de trabajo que mantienen un `claude` vivo a la vez. Por encima del tope, se suspenden los que llevan más tiempo inactivos; la creación de un espacio de trabajo nuevo nunca se bloquea. A diferencia de los TTL de inactividad, este valor por defecto **sí** depende del host, así que se calcula a partir de `MemTotal` (primero el límite del cgroup) en lugar de distribuirse como una constante. `0` lo desactiva. Véase `docs/specs/resident-cap.md`. | automático (`max(2, MemTotal_GiB × 0.4 / 0.45)`) |
| `SESSION_TIMEOUT_SECONDS` | Tiempo de espera por inactividad de la sesión. Número entero **≥ 1**; de lo contrario, el arranque se detiene indicando la clave (#1119). Un valor de más de 18 dígitos se ignora con un WARNING que indica la clave, y se usa el valor por defecto (#1256) | `300` |
| `CLORD_IDLE_SLEEP_HOURS` | Horas de inactividad antes de que un espacio de trabajo se suspenda (se admiten fracciones). `0` lo desactiva | `4` |
| `CLORD_IDLE_STOP_DAYS` | Días de inactividad antes de que un espacio de trabajo se detenga. `0` lo desactiva | `7` |
| `CLORD_ORPHAN_SWEEP_DAYS` | Días antes de que se elimine un clon limpio y sin tocar al que no apunta ningún registro. `0` lo desactiva | `30` |
| `SESSION_DIR_BASE` | Dónde se guardan los clones de cada hilo. No mueva uno existente: `--resume` se indexa por la ruta del directorio | `~/.c-lord/<instance>/sessions` |
| `CLORD_INSTANCE` | El `<instance>` de arriba | el nombre del directorio del clon |
| `CLORD_GIT_MIRRORS` | `0` hace que los clones de un mismo repositorio dejen de compartir objetos de git | `true` |

**Notificaciones y eventos**

| Variable | Qué hace | Valor por defecto |
|---|---|---|
| `CLORD_OWNER_FALLBACK` | Hasta dónde llega el aviso al propietario como último recurso en los turnos que no pidió ninguna persona (webhook / CI / planificador): `all` (🟡 de fin de turno + pausas + fallos), `blocked` (pausas + fallos: silencioso cuando funciona, ruidoso cuando se rompe), `off` (nunca, ni siquiera en los fallos) | `blocked` |
| `CLORD_EVENT_LOG` | Añade a este archivo una línea JSON por cada fin de turno (`completion` / `blocked` / `failure`), después de publicarse la última respuesta, para que un agente que reparte trabajo o un CI haga `tail -F` en lugar de sondear el hilo. Solo en el lado del servidor; véase [docs/specs/turn-end-events.md](../specs/turn-end-events.md) (#917) | (desactivado) |
| `CLORD_EVENT_WEBHOOK_URL` / `CLORD_EVENT_WEBHOOK_SECRET` | Envía por POST el mismo evento como JSON a esta URL, con `X-Clord-Signature: sha256=<HMAC-SHA256 of the body>`. Sin el secreto no se envía nada. Los fallos se reintentan unas cuantas veces y nunca bloquean el turno (#917) | (desactivado) |
| `CLORD_EVENT_DEFER_MAX_SECONDS` | Cuánto tiempo se retiene un `completion` mientras siguen ejecutándose subagentes en segundo plano antes de enviarlo de todos modos | `3600` |
| `COORDINATION_CHANNEL_ID` | Canal que recibe un aviso de una línea cuando termina el turno de una sesión; también es el canal por defecto del AI Lounge. Un número más largo que un id de Discord (20 dígitos) se ignora con un WARNING al arrancar que indica la clave, como si no estuviera definido (#1256) | (opcional) |

**Subagentes**

| Variable | Qué hace | Valor por defecto |
|---|---|---|
| `CLORD_SUBAGENT_LOG` | `0` desactiva el registro de subagentes: una línea `🤖 … 開始` / `終了` por cada ejecución de un subagente; la línea de fin lleva `subagent-*.md` (el informe completo, el encargo y todas las llamadas a herramientas) y la copia que lleva la línea de inicio se sustituye mientras el agente trabaja. Véase [docs/specs/subagent-log.md](../specs/subagent-log.md) (#971) | `true` |
| `CLORD_SUBAGENT_VOICES` | `1` publica además, **con el nombre de quien habla**, los encargos del agente principal, todos los `SendMessage` entre el principal y sus agentes y cada informe final, de modo que en el hilo se puede seguir quién le dijo qué a quién. Usa un webhook cuando el bot tiene Manage Webhooks (no es obligatorio: sin él, el nombre va delante del texto). Las llamadas a herramientas se quedan en el adjunto (#971) | `false` |
| `CLORD_SUBAGENT_PROGRESS_SECONDS` | Cada cuánto se sustituye el archivo de progreso adjunto de un subagente en ejecución (solo si ha cambiado; mínimo 10) | `60` |

**Qué muestra el hilo**

| Variable | Qué hace | Valor por defecto |
|---|---|---|
| `CLORD_RENDER_TABLE_IMAGES` | Defínala como `1`, `true` o `yes` para renderizar las tablas GFM (con barras verticales) como imágenes PNG adjuntas a los mensajes de Discord | (opcional) |
| `CLORD_SHOW_URL_EMBEDS` | Defínala como `1`/`true`/`yes`/`on` para dejar que Discord despliegue las tarjetas OGP de vista previa de enlaces de las URL que aparecen en las respuestas de Claude. Desactivado por defecto: las respuestas se mantienen compactas (sin tarjeta de vista previa). | `false` |
| `CLORD_AUTO_TOPIC` | Defínala como `1`/`true`/`yes`/`on` para que c-lord resuma con un LLM (haiku) el nombre de un hilo **nuevo** en su primer mensaje, el comportamiento anterior a #705. Desactivado por defecto: el hilo conserva el nombre con el que se abrió, y `/thread-rename` (sonnet) lo vuelve a resumir cuando se le pide. | `false` |
| `CLORD_SESSION_STATUS_BOARD` | Defínala como `1`/`true`/`yes`/`on` para mostrar el panel 📊 Session Status en `DISCORD_CHANNEL_ID` (#761). Desactivado por defecto: un arranque no publica nada allí y retira los paneles que dejaron arranques anteriores. El aviso de fin de turno en el hilo no se ve afectado. | `false` |
| `CLORD_THREAD_RETITLE` | Defínala como `1`/`true`/`yes`/`on` para cambiar el título de un hilo en mitad de la conversación cuando un LLM juzga que el trabajo ha cambiado (#121). Desactivado por defecto (#414): renombraba los hilos con demasiada facilidad. | `false` |
| `CLORD_THREAD_LAMP` | `1` añade un indicador 🟢/🟡 al nombre del hilo (la vista de la barra lateral). Desactivado por defecto porque Discord limita mucho los cambios de nombre de los hilos; el indicador en forma de reacción sobre su mensaje está siempre activo ([especificación](../specs/thread-lamp.md)) | `false` |
| `CLORD_TURN_PROGRESS` | `0` desactiva la línea de progreso del turno; `CLORD_TURN_PROGRESS_QUIET_SECONDS` cambia cuánto tiempo debe estar antes el hilo en silencio | `true` / `90` |
| `CLORD_COAUTHOR` | `0` deja de añadir trailers `Co-authored-by:` a los commits de Claude | `true` |

**Proceso y tmux**

| Variable | Qué hace | Valor por defecto |
|---|---|---|
| `CLORD_TMUX_SCREENSHOT_ROWS` | Altura, en filas, a la que `/tmux-screenshot` amplía la ventana antes de capturarla. `0` la captura tal como está | `100` |
| `CLORD_INSTANCE_ID` | Distingue las instancias de c-lord que comparten un mismo servidor tmux. Defínala solo cuando dos instancias arrancan desde el mismo directorio | la ruta real del directorio de arranque |
| `CLORD_SHUTDOWN_TIMEOUT_SECONDS` | Tras SIGTERM/SIGINT, cuánto puede tardar el proceso en salir antes de volcar sus pilas en el registro y terminar con el código de salida 70. `0` lo desactiva | `10` |

La API REST tiene sus propios ajustes (`CLORD_API_HOST`, `CLORD_API_PORT`, `CLORD_API_URL`, `CLORD_API_SECRET`, `CLORD_API_ALLOW_ANY_PEER`); véase [API REST](#api-rest).

### Vistas previas de enlaces (CLORD_SHOW_URL_EMBEDS)

Cuando la respuesta de Claude contiene una URL (p. ej., un enlace a GitHub Issues), Discord normalmente la desplegaría en una tarjeta OGP de vista previa de gran altura (título, descripción, imagen), que empuja hacia abajo la respuesta real y recarga el hilo. Por defecto, c-lord **suprime** estas vistas previas (se activa el flag `SUPPRESS_EMBEDS` en cada respuesta), de modo que el enlace queda dentro del texto y la respuesta se lee con claridad. Defina `CLORD_SHOW_URL_EMBEDS=true` para recuperar el despliegue de vistas previas por defecto de Discord.

### Tablas como imágenes (CLORD_RENDER_TABLE_IMAGES)

Cuando está activado, las tablas Markdown de las respuestas de Claude se renderizan como imágenes PNG con [Pillow](https://python-pillow.github.io/) y se adjuntan al mensaje de Discord junto con el texto. Cada celda se divide en tramos de texto y de emoji: **el texto se dibuja con una fuente compatible con CJK y los emoji se dibujan a todo color** con una fuente de emoji en color, de modo que los indicadores de estado 🟢/🔴 conservan su color.

**Se cubren todos los mensajes que publica el mirror, tanto las actualizaciones de progreso como la respuesta final** (#683). Discord no dibuja en absoluto las tablas Markdown, así que una tabla sin renderizar es una columna de caracteres `|` en bruto, y los mensajes intermedios permanecen en el hilo cuando el turno termina. Por eso, aplicar esto solo a la respuesta final dejaba al lector sin poder prever qué tablas serían legibles.

**La imagen va en el mensaje en el que está la tabla.** Una respuesta de más de 2000 caracteres se divide en varios mensajes; el PNG de cada tabla se adjunta al mensaje que contiene esa tabla (una tabla que la división corta entre dos filas se dibuja entera, en el mensaje donde empieza). Antes de #750, todas las imágenes iban al *último* mensaje, así que una tabla de la primera mitad se quedaba allí como barras en bruto mientras su imagen colgaba bajo un párrafo sin relación más abajo. `progress.txt` sigue yendo en el último mensaje.

**Como máximo 10 tablas por mensaje se convierten en imágenes.** Ese es el tope de adjuntos de Discord, y superarlo hace fallar el envío entero —texto incluido—, así que las tablas a partir de la undécima se quedan como markdown en bruto para no perder el mensaje. El límite es por mensaje, no por turno: cada mensaje de progreso tiene su propio cupo. Cuando la respuesta lleva además `progress.txt`, ese archivo ocupa un hueco y las tablas se reparten los otros nueve.

**El markdown en línea dentro de las celdas se reduce a texto plano** antes de dibujar: la imagen no puede hacerse interactiva, así que dejar la sintaxis en bruto solo añadiría ruido. `[label](url)` / `![alt](url)` pasan a ser `label` (en una imagen no se puede hacer clic en la URL y solo estorba), y de `**bold**` / `*italic*` / `` `code` `` solo se conserva el texto interior. El énfasis con guiones bajos (`_x_`, `__x__`) se deja intacto a propósito para no estropear identificadores como `_is_allowed` / `__init__`.

**`~~strike~~` es la excepción: se dibuja como una línea de tachado real.** Una imagen *sí* puede mostrar una línea sobre el texto, y Claude usa el tachado en las tablas para indicar «esta fila es la que se descarta»; Discord lo tacha en el cuerpo del mensaje, así que reducirlo a texto plano en la imagen haría que ambos no coincidieran e invertiría el significado de la tabla. La línea sigue al texto a través de los saltos de línea y se aplica tanto a una parte de la celda (`~~a~~ b`) como a la celda entera.

**Instalación:**

```bash
uv add "c-lord[table]"
# o bien: pip install "c-lord[table]"
```

> El mismo extra `[table]` es el que hace funcionar **`/tmux-screenshot`**, un comando de
> depuración que publica un PNG del panel de tmux actual (conserva los colores ANSI, la
> disposición y los indicadores de estado). Sin el extra, el comando responde con una
> indicación de instalación en lugar de una imagen.

**Fuentes compatibles (texto CJK + emoji en color):**

c-lord renderiza el texto y los emoji por separado, y elige la primera fuente que encuentra de cada grupo:

| Ruta del archivo de fuente | Grupo / Notas |
|---|---|
| `~/.local/share/fonts/NotoSansJP.ttf` | **Texto** — recomendada para japonés |
| `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` | Texto — cubre JP / ZH / KO |
| `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | Texto — último recurso (solo latín) |
| `~/.local/share/fonts/NotoColorEmoji.ttf` | **Emoji en color** — 🟢🔴🟡✅⚠️ se renderizan a todo color |
| `/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf` | Emoji en color — ruta alternativa del sistema |
| `~/.local/share/fonts/NotoEmoji-Regular.ttf` | Emoji monocromo — alternativa si no hay fuente en color (sin color, pero sin tofu) |

Si no hay ninguna fuente de emoji en color, c-lord recurre a una fuente de emoji monocroma (la forma de los glifos, sin color) y, después, a dibujar el carácter en bruto. Use la Noto Color Emoji **CBDT**: Pillow la renderiza con `embedded_color=True`.

> **¿Ve recuadros vacíos (□) en lugar de texto japonés en una captura?** Es que falta
> una fuente en *esta* máquina o para este usuario, no un error: no se encontró ninguna de
> las rutas de arriba, así que el renderizado recurrió a una fuente solo latina. Es una
> instalación por usuario y por host (`~/.local/share/fonts/`), así que no se traslada a
> otra cuenta de usuario de Linux ni a un host nuevo, aunque ya funcione en otro
> sitio.
>
> **Si no se siente cómodo ejecutando comandos de terminal, no pasa
> nada: entregue el bloque de abajo a su asistente de IA** (su sesión de Claude Code,
> o el propio hilo de Claude en c-lord) y pídale que lo ejecute por usted.
> Está escrito para que una IA pueda ejecutarlo sin supervisión con seguridad: solo escribe
> en su propio directorio personal, no necesita `sudo` y no toca nada fuera de
> `~/.local/share/fonts/`.

**Instalación rápida para japonés + emoji en color (Linux):**
```bash
mkdir -p ~/.local/share/fonts
# Texto japonés
curl -Lo ~/.local/share/fonts/NotoSansJP.ttf \
  https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJK-Regular.ttc
# Emoji en color (CBDT)
curl -Lo ~/.local/share/fonts/NotoColorEmoji.ttf \
  https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf
fc-cache -fv
```

Las celdas largas se ajustan a un ancho acotado (`MAX_COL_WIDTH`, que tiene en cuenta el ancho de visualización, de modo que los caracteres CJK cuentan doble) para que las tablas anchas sigan siendo legibles en el móvil.

> **Nota para otros idiomas CJK y otras escrituras (chino, coreano, árabe, tailandés, etc.):** instale una fuente que cubra la escritura en cuestión (p. ej., Noto Sans SC para chino simplificado, Noto Sans KR para coreano) y añada su ruta a la lista `_JP_FONT_PATHS` de `c_lord/discord_ui/table_renderer.py`. Se agradecen las contribuciones que añadan detección de fuentes para varios idiomas.

## Tareas programadas

Una tarea programada es un prompt que c-lord ejecuta por usted a intervalos fijos. c-lord solo se ocupa del *cuándo*; todo lo relativo al *qué* —qué comprobar, qué publicar, cómo no repetirse— está en el prompt. Las tareas se guardan en SQLite y se añaden, modifican y eliminan a través de la [API REST](#api-rest) mientras el bot está en marcha, así que no hace falta ni un cambio de código ni un reinicio. La forma habitual de crear una es pedirle a Claude, en un hilo, que la registre; Claude tiene la dirección de la API en `CLORD_API_URL`:

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

`name`, `prompt`, `interval_seconds` y `channel_id` son todos obligatorios, y el canal debe estar vinculado a un repositorio con `/clord-init`; `working_dir` y `run_immediately` son opcionales. `PATCH /api/tasks/{id}` con `{"enabled": false}` pausa una tarea sin eliminarla. Un único bucle se despierta cada 30 segundos e inicia lo que toque; una tarea cuya ejecución anterior sigue en marcha se omite, no se duplica.

Cada ejecución publica un mensaje `🔄 **[Scheduled]** <task name>`, abre un hilo sobre él, crea una ventana de tmux en el `working_dir` de la tarea y ejecuta Claude allí; la respuesta llega al hilo como en cualquier otro turno. Cómo debe ser una ejecución programada (y qué se considera un error) está descrito en [docs/specs/scheduled-tasks.md](../specs/scheduled-tasks.md).

## Automatización con GitHub y CI

Cualquier cosa que pueda publicar en un webhook de Discord puede encargarle trabajo a Claude. Hay dos maneras, según quién escriba el prompt.

**El remitente escribe el prompt.** Un mensaje de webhook que empieza por `!clord`, en un canal vinculado a un repositorio, abre una sesión exactamente igual que lo haría `/clord`. Tener la URL del webhook equivale a tener el permiso, así que trate la URL como un secreto.

**El prompt está fijado de su lado.** `WebhookTriggerCog` asocia un prefijo de mensaje a un prompt que usted definió en el código. El webhook solo puede elegir *qué* disparador se activa; no puede aportar texto sobre el que Claude actúe. Es la opción más segura para CI. El cog no se registra por defecto; añádalo en [su propio bot](#usar-c-lord-como-biblioteca):

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
        triggers={"🔄 docs-sync": docs_sync},      # prefijo del mensaje → qué ejecutar
        channel_ids={123456789012345678},          # escuchar solo aquí
        allowed_webhook_ids={234567890123456789},  # solo este webhook
    )
)
```

Un `WebhookTrigger` se salta las comprobaciones de permisos de Claude Code salvo que usted pase `dangerously_skip_permissions=False`, porque no hay nadie para responder a una solicitud; una razón más para restringir `channel_ids` y `allowed_webhook_ids`.

**Qué ocurre cuando se activa un disparador:** el mensaje del webhook recibe un hilo, Claude se ejecuta en su propia ventana de tmux en el `working_dir` del disparador (o en el del runner) y su respuesta se publica en ese hilo. Si la ventana no se puede crear (p. ej., tmux no está disponible en el host), la ejecución no empieza: el hilo dice por qué, el mensaje del webhook recibe ❌ y se menciona al propietario del bot (#629).

### Ejemplo: sincronización automática de la documentación (impleméntelo en su propio bot)

> **Nota:** Este es un patrón que puede implementar en su propio bot; c-lord no incluye flujos de sincronización de documentación. Como implementación de referencia, véase [EbiBot](https://github.com/ebibibi/discord-bot).

En cada push a `main`, su bot puede hacer que Claude Code:
1. Descargue los últimos cambios y analice el diff
2. Actualice la documentación en inglés
3. La traduzca al japonés (o a los idiomas de destino que quiera)
4. Cree un PR con un resumen bilingüe
5. Active el auto-merge, que fusiona automáticamente cuando el CI pasa

**GitHub Actions (añádalo a su repositorio):**

```yaml
# .github/workflows/docs-sync.yml
on:
  push:
    branches: [main]
jobs:
  ask-claude:
    # Los commits de documentación del propio Claude llevan [docs-sync]; sin esta línea volverían a lanzar el workflow
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

El lado del bot es el `WebhookTriggerCog` mostrado arriba, con `🔄 docs-sync` como disparador. Cuando el PR del propio Claude se fusiona en `main`, ese push volvería a lanzar el workflow. La línea `if:` corta el bucle, siempre que su prompt le indique a Claude que ponga `[docs-sync]` en los mensajes de sus commits.

### Enterarse de cuándo termina un turno

Un script que encargó trabajo no tiene por qué sondear el hilo. Con `CLORD_EVENT_LOG` o `CLORD_EVENT_WEBHOOK_URL` definidos, c-lord emite un evento por cada turno terminado —`completion`, `blocked` o `failure`— después de publicarse la última respuesta ([especificación](../specs/turn-end-events.md)).

### Actualizar el bot desde un webhook

`AutoUpgradeCog` es para un bot que tiene c-lord instalado como paquete. Igual que `WebhookTriggerCog`, lo añade usted mismo:

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

Cuando llega un mensaje de webhook que empieza por `trigger_prefix` (por defecto, `🔄 upgrade`), el cog abre un hilo e informa allí de cada paso:

1. Ejecuta `uv lock --upgrade-package <package_name>` y después `uv sync` en `working_dir` (sustituya cualquiera de los dos con `upgrade_command` / `sync_command`).
2. Anota qué hilos tienen un turno en ejecución y luego espera a que termine el trabajo en curso; se consulta a todos los cogs que exponen una propiedad `active_count` (el protocolo `DrainAware`), incluidos los suyos. Pasado `drain_timeout` (300 s), deja de esperar.
3. Ejecuta `restart_command`. Si no hay ninguno, no se reinicia nada y el hilo dice que la actualización ha terminado.

Tras el reinicio, los hilos anotados en el paso 2 reciben un aviso discreto de una línea que dice que c-lord se reinició. A Claude **no** se le vuelve a enviar ningún prompt: los procesos `claude` viven en tmux y nunca se detuvieron, así que no hay nada que reanudar (#406).

`upgrade_approval=True` pregunta antes del paso 1, y `restart_approval=True` pregunta una vez instalada la versión nueva, antes de los pasos 2 y 3; a cualquiera de las dos se responde con una reacción ✅ en el hilo de la actualización o con el botón Approve. `slash_command_enabled=True` añade `/upgrade`, que inicia el mismo proceso sin webhook. Solo pueden ejecutarlo los usuarios autorizados a manejar el bot. Funciona en un canal o en un hilo —si se ejecuta en un hilo, el hilo de la actualización se abre en el canal padre— y, si ya hay una actualización en marcha, se lo indica.

Un c-lord que se ejecuta desde su propio clon no necesita nada de esto: `git pull && systemctl --user restart c-lord.service`.

## API REST

Es el plano de control de c-lord para las notificaciones, las tareas programadas y la creación de sesiones. Arranca en cada ejecución; no depende de cómo se entregan las respuestas (#712/#543). Requiere aiohttp:

```bash
uv add "c-lord[api]"
```

Escucha en `CLORD_API_HOST:CLORD_API_PORT` (por defecto, `127.0.0.1:8080`). Con `CLORD_API_PORT` **sin definir**, un puerto ocupado no es un problema: la API pasa al siguiente libre (8081, 8082, … y después cualquier puerto libre) y registra a cuál fue, de modo que dos clones en un mismo host tienen cada uno su API sin configurar nada (#258). Un puerto que usted **define** se usa tal cual: si está ocupado, el bot registra un WARNING y funciona sin la API en lugar de no arrancar. Un valor que no son dígitos ASCII (`²`, una palabra) se ignora con un WARNING que nombra `CLORD_API_PORT`, como si no estuviera definido; los espacios alrededor de los dígitos se ignoran (#1218). Un número superior a 65535 es un puerto definido por usted que no se puede usar: un WARNING nombra `CLORD_API_PORT` y el bot funciona sin la API (#1237). Del mismo modo, una entrada de `CLORD_TRUSTED_BOT_IDS` que no son dígitos ASCII, o que es más larga que un id de Discord (20 dígitos), se ignora con un WARNING al arrancar, y los demás id de la lista siguen funcionando. Eso incluye los dígitos de ancho completo (`１２３`), que antes se leían como el id 123: escriba el id con dígitos ASCII.

En cada sesión, a Claude se le indica dónde está la API mediante la variable de entorno `CLORD_API_URL`: el puerto en el que realmente se abrió, así que nunca hay que mantenerla sincronizada a mano con `CLORD_API_PORT`. Defina `CLORD_API_URL` usted mismo solo para dirigir las sesiones a otro sitio (un proxy inverso, por ejemplo). Cuando la API no está en ejecución, las sesiones no reciben ningún `CLORD_API_URL`.

**Solo atiende al usuario Unix que ejecuta el bot** (#457). `POST /api/spawn` inicia una sesión de Claude Code, así que alcanzar este puerto equivale a tener un shell como ese usuario, y el loopback es una frontera de *red*, no de *UID*: todas las cuentas de un host multiusuario pueden conectarse a `127.0.0.1`. Por eso el UID del otro extremo se lee de `/proc/net/tcp` y se compara con el del bot, también en `/api/health`; todos los demás reciben `403`. No hay nada que configurar: sus propias llamadas siguen funcionando tal como están. Para dar acceso a otro usuario o a otro host, defina `CLORD_API_SECRET` y haga que envíen `Authorization: Bearer …`. Véase [docs/SECURITY.md](../SECURITY.md#rest-api-exposure-712-457).

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/health` | Comprobación de que está vivo |
| POST | `/api/spawn` | Abre un hilo nuevo e inicia Claude en él con `prompt` (`channel_id` y `thread_name` opcionales). Responde en cuanto el hilo existe |
| POST | `/api/threads/{thread_id}/messages` | Publica `content` en un hilo |
| POST | `/api/threads/{thread_id}/sleep` | Suspende el espacio de trabajo de ese hilo, como lo haría la suspensión por inactividad |
| POST | `/api/notify` | Publica una notificación (`message`; `title`, `color` y `channel_id` opcionales) |
| POST | `/api/schedule` | Publica una notificación más tarde (`message`, `scheduled_at` en ISO 8601) |
| GET | `/api/scheduled` | Lista las notificaciones que siguen pendientes |
| DELETE | `/api/scheduled/{id}` | Cancela una |
| POST | `/api/tasks` | Registra una [tarea programada](#tareas-programadas) |
| GET | `/api/tasks` | Lista las tareas |
| PATCH | `/api/tasks/{id}` | Modifica una tarea: `enabled`, `prompt`, `interval_seconds`, `working_dir` |
| DELETE | `/api/tasks/{id}` | Elimina una tarea |
| POST | `/api/mark-resume` | Registra un hilo antes de que usted mismo reinicie el bot, para que reciba el aviso de reinicio en el siguiente arranque |
| GET | `/api/lounge` | Lee los mensajes recientes del AI Lounge (a las sesiones no se les habla del lounge, así que aquí no se publica nada salvo que usted lo prepare — #758) |
| POST | `/api/lounge` | Publica un mensaje en el AI Lounge (con `label` opcional); las sesiones de c-lord no lo llaman por sí solas (#758) |

Todos los campos, códigos de estado y casos límite están en [docs/COMMANDS.md](../COMMANDS.md#rest-api). Dos ejemplos, ejecutados como el propio usuario Unix del bot, de modo que no hace falta ninguna cabecera:

```bash
# Iniciar una sesión desde un script: la respuesta trae el ID del hilo
curl -X POST http://127.0.0.1:8080/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review the newest open PR and summarise the risks", "thread_name": "PR review"}'

# Avisar al canal principal de que ha ocurrido algo
curl -X POST http://127.0.0.1:8080/api/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "CI", "message": "Nightly build is green"}'
```

## Usar c-lord como biblioteca

c-lord es un paquete de Python corriente, y `c-lord start` no es más que un programa construido sobre él. Para integrarlo en un bot propio —con sus propios cogs a su lado— instálelo y llame a `setup_bridge()`:

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
        # await bot.add_cog(YourCog(bot))   ← sus propios cogs van aquí
        await bot.start(os.environ["DISCORD_BOT_TOKEN"])


asyncio.run(main())
```

`setup_bridge()` registra los cogs estándar de c-lord; con la llamada de arriba son nueve: el chat, la vinculación de canal a repositorio, las skills, los comandos de espacio de trabajo, la limpieza a los 30 días, el planificador, el espejo de la transcripción, el relevo de inicio de sesión y `/version`. Es deliberado: una función estándar que se añada a c-lord más adelante llega a su bot cuando actualiza el paquete, sin cambiar su código. `WebhookTriggerCog` y `AutoUpgradeCog` no están entre ellos; añada esos dos usted mismo con `bot.add_cog()`, como se muestra arriba.

```bash
uv lock --upgrade-package c-lord && uv sync
```

Use `ClaudeDiscordBot` como clase del bot: los cogs de c-lord leen cosas que esa clase lleva consigo, como el canal principal. El punto de entrada independiente, [`c_lord/main.py`](../../c_lord/main.py), es la referencia para lo demás: la lectura del `.env`, los bloqueos de instancia única, el arranque de la API REST y el apagado al recibir una señal.

## Arquitectura

### Cómo se corresponde Discord con tmux

La idea «hilo de Discord = sesión de Claude Code» se apoya en una disposición de tmux. Saber dónde vive realmente un hilo facilita mucho la depuración y la integración (puede hacer `tmux attach` para ver una sesión en vivo):

| Unidad | tmux | Correspondencia |
|------|--------------|---------|
| Repositorio al que está vinculado un hilo | sesión de tmux | 1:1 |
| Hilo | ventana de tmux | 1:1 |

- **Las sesiones solo nacen en un canal.** `/clord` abre un hilo *nuevo*; dentro de un hilo continúa la sesión de ese hilo, ofrece reconectar una cuyo registro se limpió o se niega si el hilo nunca fue de c-lord (#551). Un hilo de conversación corriente no se puede convertir en una sesión de Claude: por diseño, no existe ningún comando que lo adopte. Véase [docs/COMMANDS.md](../COMMANDS.md#chat--sessions).
- **1 repositorio = 1 sesión de tmux, no 1 canal.** El nombre de la sesión sale del repositorio al que está vinculado el hilo: el suyo propio cuando lo tiene (`/clord-thread-init`, o `/clord repo:<url>`) y, si no, el del canal (`/clord-init`); los canales sin vinculación recurren a la sesión por defecto `clord`. Así, un canal cuyos hilos están vinculados a repositorios distintos se reparte entre varias sesiones, y dos canales vinculados al mismo repositorio comparten una. Dónde vive un hilo concreto —y por qué se decide así— se detalla en [docs/specs/tmux-layout.md](../specs/tmux-layout.md).
- **1 hilo = 1 ventana de tmux = 1 sesión de Claude Code.** Cada hilo tiene su propia ventana (`w1`, `w2`, …) en la que se ejecuta una sesión de Claude Code. Por eso su ir y venir en un hilo *es* el ir y venir con esa sesión de Claude: las respuestas la continúan mediante `--resume`. Los números de ventana son únicos **dentro de una sesión**, no dentro de un canal.

Para el «porqué» de este diseño, véase [docs/PHILOSOPHY.md](../PHILOSOPHY.md).

### Cómo llega una respuesta a Discord

```text
usted, en un hilo de Discord                  Claude Code, en la ventana de tmux de ese hilo
        │                                                    │
        │  su mensaje se escribe en el panel  ──────────────▶│
        │                                                    │  trabaja: lee, edita, ejecuta herramientas
        │                                                    ▼
        │                          ~/.claude/projects/<dir>/<session>.jsonl
        │                          (la transcripción que Claude Code escribe por sí mismo)
        │                                                    │
        ◀──── c-lord sigue ese archivo y publica lo que dice ┘
```

c-lord no lee la respuesta de Claude del terminal ni le pide a Claude que la publique. Lee la transcripción que Claude Code ya mantiene y la reenvía. Con la lectura de la pantalla se colaban una y otra vez trozos de la interfaz del terminal en Discord; pedirle a Claude que publicara hacía que se perdiera un turno entero cada vez que se le olvidaba. La transcripción no tiene ninguno de los dos problemas, y es la única vía que sigue una respuesta.

El panel se sigue vigilando, pero no por el texto: para detectar que un turno ha terminado, para convertir en botones de Discord un menú que muestra Claude Code (`AskUserQuestion`, aprobación del plan), para aceptar las solicitudes de permiso de Claude Code y para avisar de un prompt que c-lord no reconoce. [docs/ARCHITECTURE.md](../ARCHITECTURE.md) recorre todo esto, y [docs/DESIGN_DECISIONS.md](../DESIGN_DECISIONS.md) recoge el porqué.

### Dónde está cada cosa

```text
c_lord/
  cli.py            el comando `c-lord`: setup, start, version
  main.py           punto de entrada independiente: lee el .env, toma los bloqueos, lo arranca todo
  setup.py          setup_bridge(): registra los cogs estándar en una sola llamada
  bot.py            ClaudeDiscordBot
  tmux.py           sesiones y ventanas de tmux; arrancar claude, escribir en el panel
  session_dir.py    un clon de git por hilo
  claude/           runner por turno sobre el panel de tmux, ajustes de la CLI, uso del contexto
  transcript/       seguimiento de la transcripción de Claude Code y su formato para Discord; registro de subagentes
  cogs/             chat, vinculación canal↔repositorio, skills, comandos de espacios de trabajo, planificador,
                    disparadores de webhook, actualización automática, mirror de la transcripción
  discord_ui/       indicador de estado, botones de preguntas, línea de progreso del turno, imágenes de tablas y del panel
  database/         SQLite: sesiones, vinculaciones, tareas programadas, notificaciones, menús pendientes
  ext/api_server.py API REST
  skills/           la skill discord-read que se coloca en cada sesión
deploy/             la unidad de systemd
scripts/            install-systemd.sh, staging.sh, herramientas de evidencias y de publicación de versiones
docs/               guías, y docs/specs/: cómo debe comportarse cada función
```

### Qué es c-lord y qué no es

- **Un puente, no una IA.** c-lord no tiene prompts, herramientas ni memoria propios. Todo eso es de Claude Code, y lo que usted tenga configurado para Claude Code —`CLAUDE.md`, skills, servidores MCP, hooks— funciona en un hilo porque es el mismo programa.
- **La CLI real, no la API.** Cada hilo ejecuta el `claude` interactivo que usted ejecutaría en un terminal. c-lord nunca llama a la API de Anthropic.
- **tmux se queda.** c-lord no sustituye a tmux; le permite gestionar tmux desde Discord. El panel siempre está ahí para conectarse a él.
- **Sesiones que sobreviven a las conexiones.** Claude se ejecuta en el servidor. Que su portátil se suspenda, que su tren entre en un túnel o que el bot se reinicie no lo detiene.
- **Usted juzga desde Discord, así que Discord tiene que mostrar lo suficiente para juzgar.** El progreso, los resultados, los archivos y los fallos llegan todos al hilo, y un fallo dice qué falló y qué hacer a continuación en lugar de quedarse en silencio.
- **Nada que configurar en el caso habitual.** Las funciones nuevas vienen activadas por defecto con ajustes razonables, y lo único que hace quien usa el paquete es actualizarlo.

El razonamiento que hay detrás está en [docs/PHILOSOPHY.md](../PHILOSOPHY.md) (en japonés).

## Seguridad

Hablar con c-lord significa ejecutar comandos en la máquina en la que vive. Los valores por defecto parten de esa base.

- **Autorización de usuarios** — `allowed_user_ids` / `CLORD_ALLOWED_ROLE` restringen quién puede invocar a Claude. **Si no se configura ninguno de los dos, solo el propietario de la aplicación de Discord puede usar el bot**: hablar con c-lord ejecuta comandos de shell en su host, así que «sin configurar» no significa «todo el mundo» (#713). `CLORD_ALLOW_ANYONE=1` lo abre a todo el servidor a propósito (y lo dice al arrancar). Véase [docs/specs/authorization-default.md](../specs/authorization-default.md)
- **La API REST solo responde al usuario Unix del propio bot**, salvo que usted defina `CLORD_API_SECRET` (véase [API REST](#api-rest)).
- **Sin shell de por medio.** Todos los procesos que inicia c-lord se inician con una lista de argumentos (`asyncio.create_subprocess_exec`), nunca a través de un shell, y un prompt se pasa después de un `--` para que no pueda interpretarse como un flag.
- **Se comprueba antes de usarse.** Un ID de sesión debe tener el aspecto de uno antes de llegar a `--resume`; los nombres de las skills y las URL de los repositorios se validan del mismo modo.
- **El token del bot no se filtra a las sesiones por herencia.** Se elimina del entorno de tmux y del entorno de la línea de comandos de `claude`. Esto evita accidentes, no a una sesión decidida: el `.env` es un archivo que el mismo usuario Unix puede leer.

[docs/SECURITY.md](../SECURITY.md) contiene el modelo de amenazas y los riesgos que se aceptan a propósito.

## Desarrollo

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup                                # una vez por clon: activa el hook de pre-commit

uv run pytest tests/ -v --cov=c_lord
uv run ruff check c_lord/ && uv run ruff format --check c_lord/
uv run pyright c_lord/
```

El CI ejecuta esas comprobaciones en Python 3.10, 3.11 y 3.12. Usan mocks, así que un CI en verde no demuestra que un cambio funcione contra un Discord real y un tmux real; por eso, antes de fusionarse, un cambio también tiene que mostrarse funcionando en un bot de staging. [CONTRIBUTING.md](../../CONTRIBUTING.md) explica qué necesita un pull request; [docs/STAGING.md](../STAGING.md) explica los bots de staging.

## Cómo se construye este proyecto

**Todo este código base fue escrito por [Claude Code](https://docs.anthropic.com/en/docs/claude-code)**, el agente de codificación con IA de Anthropic. El autor humano ([@ebibibi](https://github.com/ebibibi)) proporcionó requisitos y dirección en lenguaje natural, pero no leyó ni editó manualmente el código fuente.

El párrafo anterior describe el proyecto original, claude-code-discord-bridge, que aportó a c-lord su arquitectura inicial bajo la licencia MIT. Esta obra derivada la mantiene ahora [@yousan](https://github.com/yousan), que define los requisitos, revisa los pull requests y aprueba todos los cambios, mientras que Claude Code se encarga de la implementación.

**Gracias, [@ebibibi](https://github.com/ebibibi).** c-lord comenzó a partir del código de [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge), publicado por Masahiko Ebisuda bajo la licencia MIT. Su aviso de copyright y el texto de su licencia se conservan en [NOTICE](../../NOTICE).

En el día a día funciona así. Un cambio empieza como un Issue con criterios de aceptación. Claude Code —que normalmente se ejecuta en un hilo de c-lord, de modo que c-lord se construye con c-lord— escribe un test que falla, hace que pase, muestra el cambio funcionando en un bot de staging con capturas de pantalla y abre un pull request. Dos comprobaciones obligatorias protegen `main`, y un pull request solo se fusiona cuando se cumplen todos los puntos de la [Definition of Done](../../CLAUDE.md#definition-of-done-dod--single-source-of-truth).

La historia propia de c-lord empieza el 2026-02-22 con una copia de ese proyecto original. Los informes de errores y los pull requests son bienvenidos, tanto de personas como de agentes.

## Ejemplo Real

**[EbiBot](https://github.com/ebibibi/discord-bot)** — Un bot personal de Discord construido sobre este framework. Incluye sincronización automática de documentación (inglés + japonés), notificaciones push, watchdog de Todoist, comprobaciones de salud programadas y CI/CD con GitHub Actions. Úsalo como referencia para construir tu propio bot.

EbiBot es el bot del propio ebibibi y está construido sobre el proyecto original claude-code-discord-bridge, no sobre c-lord.

## Inspirado en

- [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge), de [@ebibibi](https://github.com/ebibibi) — La base original sobre la que se apoya este proyecto.
- [OpenClaw](https://github.com/openclaw/openclaw) — Reacciones emoji de estado, debounce de mensajes, división con conocimiento de bloques
- [claude-code-discord-bot](https://github.com/timoconnellaus/claude-code-discord-bot) — Enfoque de invocación CLI + stream-json
- [claude-code-discord](https://github.com/zebbern/claude-code-discord) — Patrones de control de permisos
- [claude-sandbox-bot](https://github.com/RhysSullivan/claude-sandbox-bot) — Modelo de hilo por conversación

## Licencia

[MIT](../../LICENSE). El aviso de copyright y la licencia del código del que partió c-lord están en [NOTICE](../../NOTICE).
