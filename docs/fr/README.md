# c-lord

[![CI](https://github.com/yousan/c-lord/actions/workflows/ci.yml/badge.svg)](https://github.com/yousan/c-lord/actions/workflows/ci.yml)
[![Python 3.10 – 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg)](../../pyproject.toml)
[![MIT license](https://img.shields.io/badge/license-MIT-green.svg)](../../LICENSE)

**Lancez plusieurs sessions Claude Code en parallèle depuis Discord — chaque fil dans son propre clone git, pour que les sessions ne partagent jamais la même copie de travail.**

c-lord fait d'un serveur Discord la salle de contrôle des sessions Claude Code qui tournent sur votre propre machine. Donnez un fil à chaque tâche, partez faire autre chose, et suivez-les toutes depuis votre téléphone, votre ordinateur portable ou un terminal — rien n'est interrompu, et tout ce qui s'est passé reste dans le fil.

<p align="center">
  <img src="../assets/demo-mock.svg" alt="Maquette : un salon Discord avec quatre fils Claude Code, dont un ouvert qui montre une demande, un message saisi dans le terminal, la réponse avec des fichiers, et un terminal attaché à la même session (tous les noms sont fictifs)" width="760">
</p>

## Pourquoi c-lord

- 🎛️ **Discord devient la salle de contrôle de nombreuses sessions Claude Code.** Un fil correspond à un vrai Claude Code interactif, qui tourne dans tmux sur votre machine — le même que celui que vous utiliseriez dans un terminal. Déposez des fichiers dans le fil et Claude les reçoit ; les fichiers que Claude produit reviennent en pièces jointes. Envie de reprendre le clavier ? Faites `tmux attach` sur cette même session.
- ⏳ **Aucune limite de durée.** Une tâche longue n'est jamais arrêtée parce qu'elle est longue. c-lord n'abandonne un tour que si l'écran de Claude est resté figé pendant 5 minutes, et même dans ce cas il ne tue pas Claude.
- 🧪 **Un espace de travail complet et séparé par fil.** Chaque fil travaille dans son propre clone de votre dépôt : il peut donc lancer ensemble son backend et son frontend, ainsi que sa propre pile `docker compose`, sans toucher à ceux d'un autre fil. c-lord sait quels conteneurs appartiennent à quel fil et les arrête avec lui. (Pour l'activer, liez le salon à un dépôt avec `/clord-init` ; les ports de l'hôte restent partagés, donnez donc à chaque pile les siens.)
- 📜 **Tout est conservé dans Discord.** Les réponses, l'activité des outils, les fichiers — et même ce que vous tapez directement dans le panneau tmux — arrivent dans le fil. Votre équipe peut suivre, et un autre agent IA peut lire l'historique pour reprendre le travail.
- 🔒 **Vous choisissez qui peut le piloter.** Par défaut, seul le propriétaire de l'application Discord peut l'utiliser (parler à c-lord, c'est exécuter des commandes sur votre machine). Ouvrez-le à un rôle ou à une liste de personnes quand vous le souhaitez.

**Pour commencer :** [Démarrage rapide](#démarrage-rapide) · [Fonctionnement](#lidée-centrale--des-sessions-parallèles-chacune-dans-son-propre-clone) · [Commandes](../COMMANDS.md) · [Configuration](#configuration) · [Architecture](#architecture)

[English](../../README.md) · [日本語](../ja/README.md) · [简体中文](../zh-CN/README.md) · [한국어](../ko/README.md) · [Español](../es/README.md) · [Português](../pt-BR/README.md) · Français

Cette page traduit le [README anglais](../../README.md) ; si les deux textes ne disent pas la même chose, c'est l'anglais qui est juste.

> **Basé sur [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge) de [@ebibibi](https://github.com/ebibibi) (Masahiko Ebisuda).** Ce projet a été créé à l'origine par ebibibi et est utilisé ici sous licence MIT. Merci pour cette excellente base.

> c-lord est un projet open source indépendant. Anthropic ne le produit pas, ne le parraine pas et ne le cautionne pas ; « Claude » et « Claude Code » sont des marques d'Anthropic. c-lord se contente de piloter le CLI Claude Code que vous avez installé et auquel vous vous êtes connecté vous-même.

**Sur cette page :** [Fonctionnement](#lidée-centrale--des-sessions-parallèles-chacune-dans-son-propre-clone) · [Démarrage rapide](#démarrage-rapide) · [Travailler dans un fil](#travailler-dans-un-fil) · [Espaces de travail](#espaces-de-travail) · [Commandes](#commandes) · [Configuration](#configuration) · [Tâches planifiées](#tâches-planifiées) · [GitHub et CI](#automatisation-github-et-ci) · [API REST](#api-rest) · [Comme bibliothèque](#utiliser-c-lord-comme-bibliothèque) · [Architecture](#architecture) · [Sécurité](#sécurité) · [Développement](#développement) · [Comment il est construit](#comment-ce-projet-est-construit)

## L'idée centrale : des sessions parallèles, chacune dans son propre clone

Chaque fil Discord est une session Claude Code à part entière, qui tourne dans sa propre fenêtre tmux sur votre serveur. Envoyez des tâches à plusieurs fils et elles s'exécutent côte à côte ; fermez votre ordinateur portable et elles continuent ; répondez dans un fil pour reprendre là où il s'était arrêté.

Ce qui empêche des sessions parallèles de se marcher dessus, c'est l'**isolation** : quand un salon est lié à un dépôt (`/clord-init`), chaque fil travaille dans son propre `git clone` indépendant. Les sessions ne partagent jamais la même copie de travail, si bien que les modifications non commitées d'un fil ne peuvent jamais apparaître dans un autre. Elles ne se rencontrent que par le dépôt distant git — branches, pushs et PR — comme le font des contributeurs humains.

```text
Fil A (feature)       ──→  Claude Code  (son propre clone)  ─┐
Fil B (revue de PR)   ──→  Claude Code  (son propre clone)   ├─→  dépôt distant git (branches · PR)
Fil C (docs)          ──→  Claude Code  (son propre clone)  ─┘
```

**Aujourd'hui, les sessions ne se coordonnent pas entre elles.** Des versions antérieures de ce README décrivaient quatre mécanismes de coordination ; voici leur état réel ([#758](https://github.com/yousan/c-lord/issues/758)) :

| Mécanisme | État réel |
|---|---|
| **Avis de concurrence** (instructions données à Claude sur le travail en parallèle) | **Non construit** — les sessions tournent dans la TUI de tmux (#53), qui n'offre aucun canal de prompt système par tour ; il n'a donc jamais été transmis, et c-lord a cessé de le construire (#766) |
| **Registre des sessions actives** | Existe en mémoire et sert à `/workspace-cleanup`, mais **les sessions ne le voient pas** (rien ne le leur envoie) |
| **Salon de coordination** | **Désactivé par défaut.** Quand `COORDINATION_CHANNEL_ID` est défini, il publie un avis d'une ligne à la fin du tour d'une session ; il n'y a pas d'événement de début |
| **AI Lounge** | Les points d'accès REST fonctionnent, mais **rien n'indique à Claude que le lounge existe** ; en pratique, rien n'y est donc publié — voir [AI Lounge](#ai-lounge-non-transmis-aux-sessions) |

En résumé : des copies de travail séparées, pas de coordination en direct. Tout ce qui se trouve hors des clones — une même branche distante, un port ou une base de données partagés, un bot de staging — peut toujours entrer en collision. Donnez à chaque fil sa propre branche.

### AI Lounge (non transmis aux sessions)

> **État : non branché** ([#758](https://github.com/yousan/c-lord/issues/758)). Le lounge était conçu pour être injecté dans chaque session comme contexte système éphémère. Comme les sessions tournent dans la TUI de tmux (#53), il n'existe aucun canal par tour pour cela, et c-lord ne le construit donc pas (#766) : Claude n'est jamais informé de l'existence du lounge et, en pratique, rien n'y est publié.

Les points d'accès REST fonctionnent toujours, vous pouvez donc utiliser le lounge vous-même — par exemple en le décrivant dans le `CLAUDE.md` de votre dépôt :

```bash
# Publier une note (stockée dans SQLite ; également transmise au salon du lounge, qui est par défaut COORDINATION_CHANNEL_ID)
curl -X POST "http://localhost:8080/api/lounge" \
  -H "Content-Type: application/json" \
  -d '{"message": "Starting auth refactor on feature/oauth", "label": "feature dev"}'

# Lire les notes récentes
curl "http://localhost:8080/api/lounge"
```

## Démarrage rapide

Il vous faut une machine Linux (WSL2 convient) qui reste allumée, avec les éléments suivants installés :

- Python 3.10 ou plus récent et [uv](https://docs.astral.sh/uv/)
- `git` et `tmux`
- le [CLI Claude Code](https://docs.anthropic.com/en/docs/claude-code), déjà connecté — lancez `claude` une fois à la main pour vous en assurer
- un serveur Discord auquel vous avez le droit d'ajouter un bot

### Étape 1 — Créer le bot Discord

1. Ouvrez le [Discord Developer Portal](https://discord.com/developers/applications) et créez une **New Application**.
2. Sur sa page **Bot**, activez **Message Content Intent** (sous *Privileged Gateway Intents*) et copiez le **token** du bot.
3. Sous **OAuth2 → URL Generator**, cochez les scopes `bot` et `applications.commands`, puis ces permissions du bot :

   | Permission | Utilisée pour |
   |---|---|
   | View Channel (Voir le salon) | voir le salon, tout simplement (voir la remarque ci-dessous) |
   | Send Messages, Send Messages in Threads (Envoyer des messages, Envoyer des messages dans les fils) | les réponses |
   | Create Public Threads (Créer des fils publics) | un fil par session |
   | Read Message History (Voir les anciens messages) | rattraper les messages publiés pendant l'absence du bot |
   | Add Reactions, Manage Messages (Ajouter des réactions, Gérer les messages) | le voyant d'état sur votre message, et son effacement |
   | Manage Webhooks (Gérer les webhooks) *(facultatif)* | les messages des sous-agents sous leur propre nom (`CLORD_SUBAGENT_VOICES=1`) |

4. Ouvrez l'URL générée et ajoutez le bot à votre serveur.

> **Salons privés ou à permissions restreintes :** si un salon refuse **Voir le salon** (View Channel)
> à `@everyone` (un salon privé), l'invitation à l'échelle du serveur ci-dessus *ne suffit pas*
> — le refus au niveau du salon l'emporte dans Discord. Vous devez ajouter le **rôle du bot**
> (ou le bot en tant que membre) aux permissions spécifiques de ce salon et lui accorder au moins
> **Voir le salon**, **Créer des fils publics**, **Envoyer des messages** et **Envoyer des
> messages dans les fils**. Accorder seulement « Gérer les fils » **ne sert à rien**.
>
> Quand ces permissions manquent au bot sur un salon, **`/clord` répond (par un message
> éphémère) en indiquant les permissions exactes à accorder** (#443) — une commande slash parvient
> au bot, qu'il voie le salon ou non. Corrigez les permissions avant de continuer : sans
> **Voir le salon**, le bot ne reçoit aucun message de ce salon, et ce que vous écririez dans
> un fil de ce salon n'arriverait jamais jusqu'à Claude.

### Étape 2 — Lancer l'assistant de configuration

L'assistant écrit `.env` pour vous ; il n'y a rien à modifier à la main.

```bash
# Directement depuis GitHub, sans rien installer :
uvx --from "git+https://github.com/yousan/c-lord.git" c-lord setup

# Ou depuis un clone (c'est aussi ce qu'il vous faut pour l'exécuter en tant que service) :
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv run c-lord setup
```

Il pose six questions, dans cet ordre :

1. **CLI Claude Code** — il vérifie seulement que `claude` peut être lancé, et vous prévient si ce n'est pas le cas.
2. **Token du bot** — le token est vérifié auprès de l'API Discord (un token collé sous la forme `Bot xxx` est enregistré comme `xxx` ; après 3 réponses vides ou invalides, l'assistant s'arrête sans écrire `.env`).
3. **Salon** — il liste les salons textuels que le bot peut voir ; choisissez-en un par son numéro (aucun identifiant à copier), ou collez un identifiant de salon numérique (une URL de salon, ou des chiffres comme `²`, entraîne une nouvelle demande). Cette valeur devient `DISCORD_CHANNEL_ID`, le salon principal du bot.
4. **Votre identifiant d'utilisateur Discord** — facultatif ; c'est la personne qui sera @-mentionnée. Un nom d'utilisateur à la place de l'identifiant numérique entraîne une nouvelle demande, puisque `c-lord start` le refuse.
5. **Répertoire de travail** — le répertoire par défaut des exécutions qui n'ont pas de clone à elles.
6. **Modèle** — `sonnet`, `opus` ou `haiku`.

Il écrit ensuite `.env` (utilisez `--env PATH` pour le placer ailleurs) et propose de démarrer le bot sur-le-champ.

### Démarrer / Arrêter

```bash
c-lord start    # démarre le bot (lit .env dans le répertoire courant)
c-lord start --env /path/to/.env   # emplacement personnalisé du .env
c-lord version  # affiche le build en cours d'exécution, p. ex. v1.4.183-bd80c47e-20260908
```

Dans un clone, préfixez ces commandes par `uv run`. Un bot démarré ainsi tourne au premier plan ; Ctrl-C l'arrête. Pour le garder en marche, voir [Exécuter en tant que service](#exécuter-en-tant-que-service-systemd---user--survit-au-redémarrage).

> **Priorité de la configuration (#324) :** les clés définies dans le fichier `.env` l'emportent
> toujours sur les variables d'environnement héritées du processus — c'est le répertoire depuis
> lequel vous démarrez qui détermine l'identité du bot. Les clés *absentes* du fichier se
> rabattent toujours sur l'environnement du processus, de sorte que les installations reposant
> uniquement sur des variables d'environnement continuent de fonctionner.

### Étape 3 — Lier un dépôt et démarrer une session

Les sessions se démarrent avec une commande slash, jamais par un message ordinaire tapé dans le salon lui-même (ceux-ci sont ignorés, ce qui permet au salon de rester un lieu de discussion normal).

```text
/clord-init repo:https://github.com/you/your-project.git     ← une fois par salon
/clord Fix the flaky login test                               ← ouvre un fil et y démarre Claude
```

`/clord-init` enregistre le dépôt sur lequel travaille le salon. Par défaut, Discord ne la propose qu'aux membres disposant de la permission **Gérer le serveur** (Manage Server). Dès lors, chaque `/clord` ouvre un nouveau fil, clone ce dépôt pour lui et démarre Claude Code dans le clone. Tout ce que vous écrivez **dans ce fil** va à la même session Claude. Pour utiliser un autre dépôt pour un fil donné, nommez-le : `/clord repo:<url> <prompt>` — cela fonctionne aussi dans un salon sans liaison.

L'hôte clone avec ses propres identifiants git ; un dépôt privé nécessite donc qu'une clé SSH ou un credential helper y soit configuré. Quand un clone échoue, le fil affiche l'erreur de git telle quelle et ce qu'il faut vérifier ([spec](../specs/workspace-setup-failure.md)).

### Exécuter en tant que service (systemd --user — survit au redémarrage)

Pour que c-lord continue de tourner en arrière-plan et **démarre automatiquement au
redémarrage de l'hôte**, installez-le comme service `systemd --user`. Depuis votre clone :

```bash
bash scripts/install-systemd.sh
```

Ce script installe [`deploy/c-lord.service`](../../deploy/c-lord.service) tel quel sous
`~/.config/systemd/user/c-lord.service`, écrit les valeurs propres à l'hôte (le chemin de ce
clone, votre `uv`, votre `PATH`) dans le drop-in
`c-lord.service.d/10-install.conf`, active le service et active le **linger** afin que le
service démarre au boot même si personne n'est connecté. Relancez-le après toute modification de
`deploy/c-lord.service`. Gérez-le ensuite avec les commandes systemd habituelles :

```bash
systemctl --user restart c-lord.service     # après un `git pull`
systemctl --user stop    c-lord.service
systemctl --user status  c-lord.service
journalctl --user -u c-lord.service -f      # journaux en direct
```

Une fois l'unité installée, **systemd est le seul à démarrer le bot de ce clone.** Ne le tuez
pas pour le relancer à la main — systemd tenterait de le relancer, buterait sur le verrou
d'instance unique, abandonnerait (`Start request repeated too quickly`) et laisserait le bot
tourner sans supervision. `bash scripts/staging.sh restart|stop` en tient compte : dans un
clone vers lequel pointe le `WorkingDirectory=` d'une unité utilisateur, il appelle
`systemctl --user restart|stop` au lieu de tuer le processus, et s'arrête sur une erreur (sans
rien tuer) quand `systemctl --user` n'est pas disponible. `bash scripts/staging.sh status`
indique si chaque processus du bot se trouve dans le cgroup de l'unité.

Pourquoi `--user` plutôt qu'un service système : c-lord pilote le `tmux` de l'utilisateur, ses
fichiers de session `~/.claude`, `uv` et le CLI `claude` ; il fonctionne donc le plus proprement
en tant que **vous**, avec votre environnement — un service root/système devrait recréer tout
cela à coups de `User=`/`Environment=`. C'est `loginctl enable-linger "$USER"` qui permet à un
service utilisateur de survivre à la déconnexion et de démarrer au boot ; l'installateur
l'exécute pour vous.

> **Installation manuelle :** copiez [`deploy/c-lord.service`](../../deploy/c-lord.service), corrigez
> les deux chemins `CHANGE-ME` (répertoire du clone + `uv`), puis
> `systemctl --user daemon-reload && systemctl --user enable --now c-lord.service && loginctl enable-linger "$USER"`.

> **Remarque pour WSL :** si `systemctl --user` renvoie `Failed to connect to bus`, la session
> D-Bus de l'utilisateur n'est pas démarrée. Vérifiez que `systemd=true` figure sous `[boot]` dans
> `/etc/wsl.conf` et redémarrez WSL (`wsl --shutdown`), ou redémarrez le bot en le tuant et en
> laissant `Restart=always` le relancer (`pkill -f c_lord.main` est dangereux avec plusieurs
> clones — tuez-le par PID).

## Travailler dans un fil

Ce qui suit décrit ce que vous voyez réellement dans Discord. Chaque point renvoie à la page qui définit le comportement attendu ; l'index complet se trouve dans [docs/specs/](../specs/README.md), et le guide d'utilisation au quotidien est [docs/USER_GUIDE.md](../USER_GUIDE.md).

### Parler à Claude

- **Un fil, une conversation.** Une réponse dans le fil poursuit la même session Claude Code (`--resume`), avec son contexte intact — aujourd'hui, demain ou après un redémarrage du bot.
- **Un nouveau message interrompt.** Si Claude travaille encore quand vous écrivez de nouveau, le tour en cours est interrompu et c'est votre nouvelle instruction qui s'exécute. Pas besoin de `/stop` au préalable.
- **Arrêter.** `/stop`, ou le bouton **⏹ Stop** placé en bas du fil tant qu'un tour est en cours, arrête le tour ainsi que les commandes shell que Claude avait laissées tourner. La conversation est conservée ; votre prochain message la poursuit ([spec](../specs/stop-button.md)).
- **Aucune limite de longueur pour ce que vous envoyez.** Les longs textes collés sont transmis en entier. Si un message ne peut vraiment pas être transmis, le fil le dit — il ne termine jamais un tour vide en silence ([spec](../specs/input-delivery.md)).
- **Fichiers en entrée.** Joignez jusqu'à 10 fichiers à un message. Chacun est enregistré dans le clone du fil (`.clord/attachments/…`, tenu à l'écart de git) et son chemin est indiqué à Claude ([spec](../specs/attachments.md)).
- **Fichiers en sortie.** Un fichier que Claude vous remet est joint au fil sous son vrai nom, jusqu'à 25 Mo par défaut ([spec](../specs/user-file-delivery.md)).
- **Les longues réponses arrivent complètes.** Une réponse qui dépasse la limite de 2000 caractères de Discord est découpée en plusieurs messages sans casser les blocs de code ([spec](../specs/long-reply-split.md)).
- **Le terminal est toujours là.** Faites `tmux attach` sur la fenêtre du fil et tapez ; ce que vous y tapez apparaît dans le fil comme venant de vous, et la réponse de Claude suit comme d'habitude ([spec](../specs/mirrored-events.md)).
- **Les messages publiés pendant que le bot était arrêté ne sont pas perdus.** Quand il se reconnecte ou redémarre, il relit ce qui est arrivé entre-temps dans ses fils et l'exécute, avec une ligne expliquant le retard ([spec](../specs/gateway-backfill.md)).
- **Les noms des fils restent les vôtres** — c-lord ne re-résume jamais de lui-même le nom d'un fil. Un fil garde le nom avec lequel il a été ouvert (seuls le préfixe `W<N> │` et le numéro `#<issue>` sont ajoutés autour) ; lancez `/thread-rename` dans le fil quand vous voulez un nouveau résumé de la conversation récente (sonnet). `CLORD_AUTO_TOPIC=1` rétablit l'ancien résumé automatique (#705)

### Savoir ce qui se passe

- **État en temps réel** — Un voyant sous forme de réaction emoji sur votre message : 🟢 en cours pendant que Claude travaille, 🟡 en attente quand c'est à vous (❌ en cas d'erreur, ⏳ / ⚠️ s'il reste silencieux pendant 10 s / 30 s). Les réactions restent réactives même en cas de forte utilisation. Le voyant 🟢/🟡 dans le nom du fil est désactivé par défaut (#329) ; définissez `CLORD_THREAD_LAMP=1` pour obtenir cette vue dans la barre latérale, plus lente et cohérente seulement à terme (#246)
- **Activité des outils** — Lue dans le transcript de Claude Code lui-même, et non publiée outil par outil : un tour qui devient silencieux affiche une seule ligne de progression (`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件` — « en cours 5:56 · 61 outils » —, voir *Ligne de progression du tour* ci-dessous), et les appels d'outils et phrases de progression du tour sont joints à la réponse sous la forme de `progress.txt` (#723)
- **Ligne de progression du tour** — Quand un tour reste silencieux pendant 90 s, une ligne en petit texte apparaît (`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`), se met à jour sur place toutes les 15 s et disparaît dès que de la vraie sortie revient ; elle indique `⏳ 待機中` (« en attente ») quand même l'activité des outils s'est arrêtée. Quand Claude écrit une phrase de progression (la ligne « ● » du panneau), la ligne affiche la plus récente à la place de l'outil (#883) — ces phrases arrivent aussi dans `progress.txt`, jamais comme des messages à part. Elle n'est jamais publiée en dehors d'un tour. Désactivez-la avec `CLORD_TURN_PROGRESS=0` (#539)
- **Utilisation du contexte** — Chaque tour se termine par un pied de message d'une ligne (`📊 7% context (69k/1.0M) · …`) : pourcentage de la fenêtre de contexte (tokens d'entrée + de cache, hors sortie), modèle, versions et coût ; il se transforme en avertissement ⚠️ au-delà de 83,5 %, seuil à partir duquel l'auto-compactage peut se déclencher au tour suivant (`docs/specs/context-footer.md`)
- **Avis de compactage** — Quand Claude Code compacte le contexte, le fil reçoit une ligne `🗜️ コンテキストを圧縮しました` (« le contexte a été compacté ») au lieu du résumé de continuation brut (#628)
- **Voyant de blocage** — Après 30 s sans activité, la réaction sur votre message passe à ⚠️ (une réflexion prolongée ou un compactage du contexte peuvent être aussi silencieux) ; aucun message n'est publié dans le fil (#473)
- **Notification de fin de tour** — Quand le tour est terminé, le fil @-mentionne la personne qui l'avait demandé, ce qui vous notifie. Un tour qui n'a produit aucune réponse le dit au lieu de prétendre être terminé ([spec](../specs/turn-end-notification.md)).
- **Notifications de dépassement de délai** — Un embed avec le temps écoulé et des indications pour reprendre, émis uniquement quand Claude est réellement coincé (panneau figé pendant toute la fenêtre de temps *et* pas au repos devant son invite) ; un tour qui s'est terminé normalement ne le déclenche jamais (#541), pas plus qu'un menu de question ou de plan qui attend votre réponse (#751)
- **Limite d'utilisation et connexion** — Atteindre la limite d'utilisation de l'abonnement est signalé comme tel, avec l'heure de sa réinitialisation ([spec](../specs/usage-limit.md)). Quand la connexion de Claude Code expire, le propriétaire reçoit un seul MP qui nomme l'hôte et les fils arrêtés, et peut se reconnecter depuis ce MP ([spec](../specs/login-alert.md)).
- **📊 Tableau Session Status** — Désactivé par défaut (#761). Définissez `CLORD_SESSION_STATUS_BOARD=1` pour maintenir dans `DISCORD_CHANNEL_ID` un tableau en direct qui liste chaque session comme 🟢 Auto-processing / 🟡 Waiting for input. Tant qu'il est désactivé, un bot qui démarre ne publie rien dans le salon et retire les tableaux laissés par des démarrages antérieurs ; la notification de fin de tour `🟡 Claude has finished … @you` dans le fil n'est pas concernée. Voir `docs/specs/session-status-board.md`

### Quand Claude vous pose une question

- **Questions** — Quand Claude appelle `AskUserQuestion`, les choix apparaissent dans le fil sous forme de boutons ou de menu déroulant, et Claude continue avec ce que vous avez choisi. Les boutons fonctionnent encore après un redémarrage du bot ([spec](../specs/ask-menu-lifecycle.md)).
- **Mode plan** — Quand Claude appelle `ExitPlanMode`, le menu d'approbation qu'affiche Claude Code est relayé vers Discord de la même manière que pour `AskUserQuestion` : les choix apparaissent sous forme de boutons, et Claude poursuit avec celui que vous choisissez (#251)
- **Demandes de permission pour les outils** — Non affichées dans Discord : c-lord accepte la demande de permission de Claude Code dans le panneau afin que le tour se poursuive. Ce que Claude a le droit de faire est déterminé par le mode de permission et les réglages avec lesquels la session tourne, et non par un bouton pour chaque outil

### Répartir le travail

- **Journal des sous-agents** — Le travail réparti entre des sous-agents à l'intérieur d'un fil reste visible dans Discord : une ligne `🤖 サブエージェント「…」開始` / `終了 · 6:12 · ツール 48 件` (« sous-agent “…” : début » / « fin · 6:12 · 48 outils ») par exécution, la ligne de fin portant `subagent-*.md` (rapport complet, mission, appels d'outils), et la copie jointe à la ligne de début étant actualisée pendant l'exécution. `CLORD_SUBAGENT_VOICES=1` ajoute les messages et le rapport de chaque agent sous son propre nom (#971, `docs/specs/subagent-log.md`)

**Quand ouvrir un nouveau fil, et quand répartir le travail à l'intérieur d'un fil** (#969) : n'ouvrez un nouveau fil que si le travail a besoin de sa propre branche, de son propre serveur de développement ou de sa propre conversation avec vous. Pour répartir un même travail — recherche, revues, tri — demandez à Claude d'utiliser des sous-agents dans le fil : aucun processus `claude` supplémentaire (≈285 Mo chacun) n'est démarré, et le début, la fin et le rapport complet de chaque sous-agent arrivent quand même dans le fil ([journal des sous-agents](../specs/subagent-log.md)). Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`) est expérimental et ses coéquipiers ne reviennent pas quand c-lord redémarre ou reprend la session ; n'utilisez pas son affichage en panneaux séparés (`--teammate-mode tmux`), qui ajoute des panneaux à la fenêtre que pilote c-lord.

### Skills, modèles, commits

- **Skills** — `/skill` exécute l'un des skills Claude Code de `~/.claude/skills/`. Le nom est proposé en autocomplétion, et un skill ajouté pendant que le bot tourne apparaît en moins d'une minute. Dans un salon, la commande ouvre un nouveau fil ; dans un fil, elle poursuit la session de ce fil.
- **Changement de modèle à l'exécution** — `/model` affiche et modifie le modèle et l'effort à l'échelle de la session, du salon ou globale (`include-existing` fait aussi basculer les sessions ouvertes à partir de leur prochain tour)
- **Qui a demandé un commit** — Les commits que Claude fait dans un fil portent des trailers `Co-authored-by:` pour l'utilisateur Discord qui a demandé ce tour et pour Claude. C'est un hook git qui s'en charge, et non une consigne dont Claude devrait se souvenir ; `CLORD_COAUTHOR=0` le désactive ([spec](../specs/commit-coauthors.md)).

## Espaces de travail

Un **espace de travail** (workspace) regroupe tout ce qui appartient à un fil : sa session Claude, son clone et tout environnement docker démarré depuis ce clone.

- **Un clone git par fil** — Quand un salon est lié à un dépôt (`/clord-init`), chaque fil travaille dans son propre `git clone` indépendant ; les sessions ne partagent rien d'autre que le dépôt distant git
- **Trois niveaux d'« arrêt », chacun incluant le précédent.** Aucune de ces opérations ne supprime l'historique de la conversation ni un volume docker ([vocabulaire](../specs/workspace-vocabulary.md)) :

  | | Ce qui s'arrête | Automatiquement | À la main |
  |---|---|---|---|
  | **Veille** (Sleep) | le processus `claude` (docker continue de tourner) | après 4 heures d'inactivité (`CLORD_IDLE_SLEEP_HOURS`) | — le message suivant le réveille sans rien afficher |
  | **Arrêt** (Stop) | + l'environnement docker | après 7 jours d'inactivité (`CLORD_IDLE_STOP_DAYS`) | `/workspace-stop`, annulé par `/workspace-start` |
  | **Suppression** (Delete) | + le clone lui-même | après 30 jours sans utilisation, voir ci-dessous | `/workspace-delete` |

- **Un plafond sur le nombre d'espaces éveillés.** Au-delà de `CLORD_MAX_RESIDENT_WORKSPACES` (calculé par défaut à partir de la mémoire de l'hôte), les espaces de travail inactifs depuis le plus longtemps sont mis en veille. Le démarrage d'un nouvel espace n'est jamais mis en attente ([spec](../specs/resident-cap.md)).
- **État du salon** — `/clord-status` liste les sessions de ce salon avec la taille du répertoire, la cible de `tmux attach` et la commande `claude --resume` ; `show_all` inclut les sessions fermées (à la manière de `docker ps -a`). Remplace les anciennes commandes `/sessions`, `/session-dirs` et `/resume-info`.
- **Nettoyage des espaces de travail** — `/workspace-cleanup` récupère les répertoires de clones qu'aucune session en cours n'utilise (`dry_run` pour un aperçu) ; un répertoire qui contient du travail (modifications non commitées, commits non poussés, fichiers ignorés tels que `.env`) n'est jamais supprimé, et la réponse indique lesquels (#1016)
- **Rangement à 30 jours, annoncé** — À chaque démarrage, les espaces de travail inutilisés depuis **30 jours** sont rangés. Cette durée suit la rétention des transcripts de Claude Code lui-même (`cleanupPeriodDays`) : à ce moment-là, Claude a de toute façon généralement oublié la conversation. Le clone n'est supprimé que s'il ne contient aucun travail non commité ; l'enregistrement qui relie le fil à sa session est conservé. Un fil encore ouvert reçoit un avis 🧹 indiquant ce qui s'est passé et ce qui a été conservé, puis il est archivé ; un fil déjà fermé est rangé sans publication, et reste donc fermé. Écrire plus tard dans un tel fil le reconnecte à ce qui se trouve encore sur le disque, ou démarre une nouvelle conversation dans le même fil. Détails : [Why a thread loses its record](../COMMANDS.md#why-a-thread-loses-its-record-the-30-day-sweep).
- **Les clones partagent les objets git.** Les clones d'un même dépôt empruntent à un miroir local unique, si bien que le clone d'un nouveau fil prend quelques secondes et peu d'espace disque. Rien ne change dans votre façon d'utiliser git ; `CLORD_GIT_MIRRORS=0` désactive ce partage ([spec](../specs/git-object-mirrors.md)).

## Commandes

Toutes les commandes sont décrites dans **[docs/COMMANDS.md](../COMMANDS.md)**. Celles que vous utiliserez le plus :

| Commande | Où | Ce qu'elle fait |
|---|---|---|
| `/clord-init repo:<url>` | salon | Lie le salon à un dépôt (sans argument : affiche les liaisons) |
| `/clord <prompt>` | salon | Ouvre un fil et démarre une session ; `repo:<url>` choisit un dépôt pour ce fil |
| `/stop` | fil | Arrête le tour en cours ; la conversation est conservée |
| `/clear` · `/compact` | fil | Repart de zéro dans le même fil · résume le contexte pour libérer la fenêtre |
| `/skill <name> [args]` | salon ou fil | Exécute un skill Claude Code |
| `/model` | partout | Affiche ou modifie le modèle et l'effort pour la session, le salon ou l'ensemble |
| `/clord-status` | partout | Les sessions de ce salon, avec la cible de `tmux attach` et la commande `claude --resume` |
| `/workspace-stop` · `/workspace-start` · `/workspace-delete` · `/workspace-cleanup` | fil / partout | Les opérations sur les [espaces de travail](#espaces-de-travail) |
| `/claude-restart` · `/resync` · `/tmux-screenshot` | fil | Redémarre le processus Claude · reconnecte le fil à son panneau · publie une image du panneau |
| `/thread-rename` | fil | Re-résume le nom du fil |
| `/clord-reattach` | fil | Reconnecte un fil rangé à ce qui se trouve encore sur le disque |
| `/version` | partout | Quel build est en cours d'exécution |

La plupart des commandes slash existent aussi sous forme de commande texte précédée de `!` (`!clord`, `!stop`, …). Ce sont celles qu'un webhook peut envoyer — Discord ne permet pas aux webhooks d'utiliser les commandes slash — et c'est par elles que des scripts et d'autres agents lancent du travail : un message de webhook commençant par `!clord`, publié dans un salon lié à un dépôt, ouvre une session.

## Configuration

Tout se règle dans `.env`. Seules les deux premières variables sont obligatoires. [.env.example](../../.env.example) est un point de départ commenté, pas la liste complète : certaines des variables des tableaux ci-dessous n'y figurent pas, et il en contient quelques-unes (pour les outils de test et de supervision) qui ne sont pas listées ici.

**Connexion**

| Variable | Ce qu'elle fait | Valeur par défaut |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Le token du bot | (obligatoire) |
| `DISCORD_CHANNEL_ID` | Identifiant du salon pour discuter avec Claude. Comme tous les réglages d'identifiant ici (`DISCORD_OWNER_ID`, `COORDINATION_CHANNEL_ID`, `EXPECTED_BOT_USER_ID`), chiffres ASCII uniquement — un lien, un nom ou `²` arrête `c-lord start` en indiquant le nom et la valeur du réglage (#1084) ; les espaces autour des chiffres sont ignorés (#1194) | (obligatoire) |
| `EXPECTED_BOT_USER_ID` | L'identifiant d'utilisateur du bot lui-même. Si le processus se connecte sous un autre bot, il s'arrête aussitôt — utile à définir quand plusieurs bots tournent sur un même hôte | (désactivé) |

**Claude Code**

| Variable | Ce qu'elle fait | Valeur par défaut |
|---|---|---|
| `CLAUDE_COMMAND` | L'exécutable Claude Code à lancer | `claude` |
| `CLAUDE_MODEL` | Modèle des nouvelles sessions. `/model` le change à l'exécution | `sonnet` |
| `CLAUDE_EFFORT` | Effort de raisonnement passé via `--effort` (`low` / `medium` / `high` / `xhigh` / `max`). Non définie, aucun flag n'est passé | (valeur par défaut du CLI) |
| `CLAUDE_PERMISSION_MODE` | Mode de permission dans lequel tournent les sessions | `acceptEdits` |
| `CLAUDE_WORKING_DIR` | Répertoire des exécutions qui n'apportent pas leur propre clone (une tâche planifiée ou un déclencheur webhook sans `working_dir`) | répertoire courant |
| `CLORD_SESSION_PLUGINS` | `0` empêche c-lord d'écarter le plugin Discord et les plugins de serveur de langage inutiles quand il démarre une session ([spec](../specs/session-plugins.md)) | `true` |

**Qui peut l'utiliser**

| Variable | Ce qu'elle fait | Valeur par défaut |
|---|---|---|
| `DISCORD_OWNER_ID` | Identifiant de l'utilisateur autorisé à piloter le bot, et @-mentionné quand Claude a besoin d'une réponse. Non définie, le bot n'autorise que le propriétaire de l'application Discord (#713) | (facultatif) |
| `CLORD_ALLOWED_ROLE` | Nom du rôle Discord dont les membres peuvent piloter le bot (OU logique avec `DISCORD_OWNER_ID`) | (facultatif) |
| `CLORD_ALLOW_ANYONE` | `1` permet à **tous** les membres du serveur de piloter le bot — c'est-à-dire d'exécuter des commandes shell sur l'hôte. Un avertissement est émis au démarrage | `false` |
| `CLORD_NOTIFY_DENIED` | `0` supprime le MP que reçoit le propriétaire de l'application quand une personne non autorisée publie dans un fil du bot (l'expéditeur ne voit jamais rien ; le refus reste journalisé — #346) | `true` |
| `CLORD_HOST_LABEL` | Le nom donné à cette machine dans le MP de perte de connexion (#1309 — pour distinguer un second hôte) | nom de l'hôte |
| `CLORD_LOGIN_RELAY` | `0` conserve le MP de perte de connexion mais retire la possibilité de se reconnecter depuis celui-ci (lien de connexion + réponse avec le code, #1310) | `true` |

**Limites et espaces de travail**

| Variable | Ce qu'elle fait | Valeur par défaut |
|---|---|---|
| `MAX_CONCURRENT_SESSIONS` | Nombre maximal de **tours exécutés en même temps**. Ce n'est pas un plafond sur les processus `claude` résidents — le sémaphore n'encadre que l'exécution d'un tour et est libéré à la fin du tour, alors que le panneau tmux continue de vivre. Pour le plafond des résidents, voir `CLORD_MAX_RESIDENT_WORKSPACES` (#576). Doit être un nombre entier **≥ 1** — `0`, les nombres négatifs et les valeurs non numériques arrêtent le démarrage en nommant la clé (#1119). Un nombre de plus de 18 chiffres est ignoré avec un WARNING qui nomme la clé, et la valeur par défaut est utilisée (#1256). | `3` |
| `CLORD_MAX_RESIDENT_WORKSPACES` | Nombre maximal d'espaces de travail qui hébergent un `claude` vivant en même temps. Au-delà du plafond, ceux qui sont inactifs depuis le plus longtemps sont mis en veille ; la création d'un nouvel espace de travail n'est jamais bloquée. Contrairement aux durées d'inactivité, cette valeur par défaut **dépend** de l'hôte ; elle est donc calculée à partir de `MemTotal` (la limite du cgroup en priorité) plutôt que livrée comme une constante. `0` la désactive. Voir `docs/specs/resident-cap.md`. | auto (`max(2, MemTotal_GiB × 0.4 / 0.45)`) |
| `SESSION_TIMEOUT_SECONDS` | Délai d'inactivité d'une session. Nombre entier **≥ 1**, sinon le démarrage s'arrête en nommant la clé (#1119). Un nombre de plus de 18 chiffres est ignoré avec un WARNING qui nomme la clé, et la valeur par défaut est utilisée (#1256) | `300` |
| `CLORD_IDLE_SLEEP_HOURS` | Heures d'inactivité avant la mise en veille d'un espace de travail (fractions autorisées). `0` la désactive | `4` |
| `CLORD_IDLE_STOP_DAYS` | Jours d'inactivité avant l'arrêt d'un espace de travail. `0` le désactive | `7` |
| `CLORD_ORPHAN_SWEEP_DAYS` | Nombre de jours avant la suppression d'un clone propre et intact vers lequel aucun enregistrement ne pointe. `0` la désactive | `30` |
| `SESSION_DIR_BASE` | Emplacement des clones par fil. Ne déplacez pas un emplacement existant : `--resume` est indexé sur le chemin du répertoire | `~/.c-lord/<instance>/sessions` |
| `CLORD_INSTANCE` | Le `<instance>` ci-dessus | nom du répertoire du clone |
| `CLORD_GIT_MIRRORS` | `0` empêche les clones d'un même dépôt de partager les objets git | `true` |

**Notifications et événements**

| Variable | Ce qu'elle fait | Valeur par défaut |
|---|---|---|
| `CLORD_OWNER_FALLBACK` | Jusqu'où va le repli vers le propriétaire pour les tours qu'aucun humain n'a demandés (webhook / CI / planificateur) : `all` (🟡 de fin de tour + pauses + échecs), `blocked` (pauses + échecs — silencieux quand tout va bien, bruyant quand ça casse), `off` (jamais, échecs compris) | `blocked` |
| `CLORD_EVENT_LOG` | Ajoute une ligne JSON par fin de tour (`completion` / `blocked` / `failure`) à ce fichier, une fois la dernière réponse publiée — pour qu'un agent répartiteur ou une CI fasse un `tail -F` au lieu d'interroger le fil. Côté serveur uniquement ; voir [docs/specs/turn-end-events.md](../specs/turn-end-events.md) (#917) | (désactivé) |
| `CLORD_EVENT_WEBHOOK_URL` / `CLORD_EVENT_WEBHOOK_SECRET` | Envoie le même événement en JSON par POST à cette URL, avec `X-Clord-Signature: sha256=<HMAC-SHA256 of the body>`. Rien n'est envoyé sans le secret. Les échecs sont retentés quelques fois et ne bloquent jamais le tour (#917) | (désactivé) |
| `CLORD_EVENT_DEFER_MAX_SECONDS` | Durée pendant laquelle un `completion` est retenu tant que des sous-agents en arrière-plan tournent encore, avant d'être envoyé malgré tout | `3600` |
| `COORDINATION_CHANNEL_ID` | Salon qui reçoit un avis d'une ligne à la fin du tour d'une session ; c'est aussi le salon par défaut de l'AI Lounge. Un nombre plus long qu'un identifiant Discord (20 chiffres) est ignoré avec un WARNING au démarrage qui nomme la clé, comme si elle n'était pas définie (#1256) | (facultatif) |

**Sous-agents**

| Variable | Ce qu'elle fait | Valeur par défaut |
|---|---|---|
| `CLORD_SUBAGENT_LOG` | `0` arrête le journal des sous-agents : une ligne `🤖 … 開始` / `終了` par exécution d'un sous-agent, la ligne de fin portant `subagent-*.md` (le rapport complet, la mission et chaque appel d'outil), et la copie jointe à la ligne de début étant remplacée pendant que l'agent travaille. Voir [docs/specs/subagent-log.md](../specs/subagent-log.md) (#971) | `true` |
| `CLORD_SUBAGENT_VOICES` | `1` publie en plus, **sous le nom de celui qui parle**, les missions données par le parent, chaque `SendMessage` entre le parent et ses agents, et chaque rapport final — pour que l'on puisse suivre dans le fil qui a dit quoi à qui. Utilise un webhook quand le bot dispose de la permission Gérer les webhooks (non requise : sans elle, le nom est placé devant le texte). Les appels d'outils restent dans la pièce jointe (#971) | `false` |
| `CLORD_SUBAGENT_PROGRESS_SECONDS` | Fréquence à laquelle le fichier de progression joint d'un sous-agent en cours est remplacé (uniquement s'il a changé ; minimum 10) | `60` |

**Ce que le fil affiche**

| Variable | Ce qu'elle fait | Valeur par défaut |
|---|---|---|
| `CLORD_RENDER_TABLE_IMAGES` | Définissez-la à `1`, `true` ou `yes` pour rendre les tableaux GFM (à barres verticales) en images PNG jointes aux messages Discord | (facultatif) |
| `CLORD_SHOW_URL_EMBEDS` | Définissez-la à `1`/`true`/`yes`/`on` pour laisser Discord déployer les cartes OGP / d'aperçu de lien pour les URL contenues dans les réponses de Claude. Désactivée par défaut — les réponses restent compactes (pas de carte d'aperçu). | `false` |
| `CLORD_AUTO_TOPIC` | Définissez-la à `1`/`true`/`yes`/`on` pour laisser c-lord résumer le nom d'un **nouveau** fil avec un LLM (haiku) à son premier message — le comportement antérieur à #705. Désactivée par défaut : le fil garde le nom avec lequel il a été ouvert, et `/thread-rename` (sonnet) le re-résume à la demande. | `false` |
| `CLORD_SESSION_STATUS_BOARD` | Définissez-la à `1`/`true`/`yes`/`on` pour afficher le tableau 📊 Session Status dans `DISCORD_CHANNEL_ID` (#761). Désactivée par défaut : un démarrage n'y publie rien et retire les tableaux laissés par des démarrages antérieurs. La notification de fin de tour dans le fil n'est pas concernée. | `false` |
| `CLORD_THREAD_RETITLE` | Définissez-la à `1`/`true`/`yes`/`on` pour renommer un fil en cours de conversation quand un LLM juge que le travail a changé (#121). Désactivée par défaut (#414) — elle renommait les fils trop volontiers. | `false` |
| `CLORD_THREAD_LAMP` | `1` ajoute un voyant 🟢/🟡 au nom du fil (la vue de la barre latérale). Désactivée par défaut, car Discord limite fortement les renommages de fils ; le voyant en réaction sur votre message est toujours actif ([spec](../specs/thread-lamp.md)) | `false` |
| `CLORD_TURN_PROGRESS` | `0` désactive la ligne de progression du tour ; `CLORD_TURN_PROGRESS_QUIET_SECONDS` modifie la durée de silence requise dans le fil avant son apparition | `true` / `90` |
| `CLORD_COAUTHOR` | `0` cesse d'ajouter des trailers `Co-authored-by:` aux commits de Claude | `true` |

**Processus et tmux**

| Variable | Ce qu'elle fait | Valeur par défaut |
|---|---|---|
| `CLORD_TMUX_SCREENSHOT_ROWS` | Hauteur, en lignes, à laquelle `/tmux-screenshot` agrandit la fenêtre avant la capture. `0` la capture telle quelle | `100` |
| `CLORD_INSTANCE_ID` | Distingue les instances de c-lord qui partagent un même serveur tmux. Ne la définissez que si deux instances démarrent depuis le même répertoire | chemin réel du répertoire de démarrage |
| `CLORD_SHUTDOWN_TIMEOUT_SECONDS` | Après SIGTERM/SIGINT, temps dont dispose le processus pour se terminer avant qu'il n'écrive ses piles d'appels dans le journal et ne sorte avec le code 70. `0` le désactive | `10` |

L'API REST a ses propres réglages (`CLORD_API_HOST`, `CLORD_API_PORT`, `CLORD_API_URL`, `CLORD_API_SECRET`, `CLORD_API_ALLOW_ANY_PEER`) — voir [API REST](#api-rest).

### Aperçus des liens (CLORD_SHOW_URL_EMBEDS)

Quand la réponse de Claude contient une URL (par exemple un lien vers une issue GitHub), Discord la déploierait normalement en une haute carte d'aperçu OGP (titre, description, image), ce qui repousse la réponse proprement dite vers le bas et encombre le fil. Par défaut, c-lord **supprime** ces aperçus (le flag `SUPPRESS_EMBEDS` est positionné sur chaque réponse) : le lien reste dans le texte et la réponse se lit proprement. Définissez `CLORD_SHOW_URL_EMBEDS=true` pour rétablir le déploiement des aperçus de liens par défaut de Discord.

### Rendu des tableaux en image (CLORD_RENDER_TABLE_IMAGES)

Quand cette option est activée, les tableaux Markdown présents dans les réponses de Claude sont rendus en images PNG à l'aide de [Pillow](https://python-pillow.github.io/) et joints au message Discord, à côté du texte. Chaque cellule est découpée en segments de texte et d'emoji : **le texte est dessiné avec une police compatible CJK et les emoji sont dessinés en couleur** à partir d'une police d'emoji en couleur, de sorte que les voyants d'état 🟢/🔴 gardent leur couleur.

**Tous les messages publiés par le miroir sont concernés — les mises à jour de progression comme la réponse finale** (#683). Discord n'affiche aucun tableau Markdown ; un tableau non rendu est donc une colonne de caractères `|` bruts, et les messages intermédiaires restent dans le fil après la fin du tour. N'appliquer ce rendu qu'à la réponse finale empêchait donc le lecteur de prévoir quels tableaux seraient lisibles.

**L'image accompagne le message qui contient le tableau.** Une réponse de plus de 2000 caractères est découpée en plusieurs messages ; le PNG de chaque tableau est joint au message qui contient ce tableau (un tableau que le découpage coupe entre deux lignes est dessiné en entier, sur le message où il commence). Avant #750, toutes les images allaient sur le *dernier* message : un tableau situé dans la première moitié y restait sous forme de barres brutes, tandis que son image pendait plus bas sous un paragraphe sans rapport. `progress.txt` va toujours sur le dernier message.

**Au plus 10 tableaux par message deviennent des images.** C'est le plafond de pièces jointes de Discord, et le dépasser fait échouer tout l'envoi — texte compris ; les tableaux au-delà du dixième restent donc en Markdown brut plutôt que de coûter le message. La limite s'applique par message, pas par tour : chaque message de progression dispose de son propre quota. Quand la réponse porte aussi `progress.txt`, ce fichier prend un emplacement et les tableaux disposent des neuf autres.

**Le Markdown en ligne à l'intérieur des cellules est réduit à du texte brut** avant le dessin — l'image ne peut pas être rendue interactive, et laisser la syntaxe brute ne ferait qu'ajouter du bruit. `[label](url)` / `![alt](url)` deviennent `label` (l'URL n'est pas cliquable dans une image et ne fait qu'encombrer), et `**bold**` / `*italic*` / `` `code` `` ne gardent que leur texte intérieur. L'emphase par tirets bas (`_x_`, `__x__`) est volontairement laissée intacte afin que des identifiants tels que `_is_allowed` / `__init__` ne soient pas abîmés.

**`~~strike~~` fait exception : il est dessiné comme un vrai trait de barré.** Une image *peut* montrer un trait à travers le texte, et Claude utilise le barré dans les tableaux pour signifier « cette ligne est celle que l'on abandonne » — Discord le barre dans le corps du message ; le réduire à du texte brut dans l'image mettrait donc les deux en désaccord et inverserait le sens du tableau. Le trait suit le texte sur les lignes repliées et s'applique aussi bien à une partie de cellule (`~~a~~ b`) qu'à la cellule entière.

**Installation :**

```bash
uv add "c-lord[table]"
# ou : pip install "c-lord[table]"
```

> Le même extra `[table]` sert aussi à **`/tmux-screenshot`** — une commande de débogage
> qui publie un PNG du panneau tmux courant (couleurs ANSI, mise en page et voyants
> d'état préservés). Sans cet extra, la commande répond par une indication d'installation
> au lieu d'une image.

**Prise en charge des polices (texte CJK + emoji en couleur) :**

c-lord rend le texte et les emoji séparément, en prenant la première police trouvée dans chaque groupe :

| Chemin du fichier de police | Groupe / Remarques |
|---|---|
| `~/.local/share/fonts/NotoSansJP.ttf` | **Texte** — recommandée pour le japonais |
| `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` | Texte — couvre JP / ZH / KO |
| `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | Texte — dernier recours (latin uniquement) |
| `~/.local/share/fonts/NotoColorEmoji.ttf` | **Emoji en couleur** — 🟢🔴🟡✅⚠️ sont rendus en couleur |
| `/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf` | Emoji en couleur — autre chemin, au niveau du système |
| `~/.local/share/fonts/NotoEmoji-Regular.ttf` | Emoji monochromes — repli en l'absence de police en couleur (pas de couleur, mais pas de « tofu ») |

Si aucune police d'emoji en couleur n'est présente, c-lord se rabat sur une police d'emoji monochrome (la forme des glyphes, sans la couleur), puis sur le dessin du caractère brut. Utilisez la version **CBDT** de Noto Color Emoji — Pillow la rend avec `embedded_color=True`.

> **Vous voyez des carrés vides (□) à la place du texte japonais dans une capture ?** Il s'agit
> d'une police manquante sur *cette* machine / pour cet utilisateur, pas d'un bug — aucun des
> chemins ci-dessus n'a été trouvé, et le rendu s'est donc rabattu sur une police limitée au
> latin. L'installation se fait par utilisateur et par hôte (`~/.local/share/fonts/`) ; elle ne
> suit donc pas vers un autre compte utilisateur Linux ni vers un nouvel hôte, même si tout
> fonctionne déjà ailleurs.
>
> **Si vous n'êtes pas à l'aise pour exécuter vous-même des commandes dans un terminal, ce
> n'est pas un problème — confiez simplement le bloc ci-dessous à votre assistant IA** (votre
> session Claude Code, ou le fil Claude de c-lord lui-même) et demandez-lui de l'exécuter pour vous.
> Il est écrit pour pouvoir être exécuté sans risque par une IA sans surveillance : il n'écrit que
> dans votre propre répertoire personnel, ne nécessite pas `sudo` et ne touche à rien en dehors de
> `~/.local/share/fonts/`.

**Installation rapide pour le japonais + les emoji en couleur (Linux) :**
```bash
mkdir -p ~/.local/share/fonts
# Texte japonais
curl -Lo ~/.local/share/fonts/NotoSansJP.ttf \
  https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJK-Regular.ttc
# Emoji en couleur (CBDT)
curl -Lo ~/.local/share/fonts/NotoColorEmoji.ttf \
  https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf
fc-cache -fv
```

Les cellules longues sont repliées à une largeur bornée (`MAX_COL_WIDTH`, qui tient compte de la largeur d'affichage : les caractères CJK comptent double) afin que les tableaux larges restent lisibles sur mobile.

> **Remarque pour les autres langues CJK et écritures (chinois, coréen, arabe, thaï, etc.) :** installez une police qui couvre l'écriture visée (par exemple Noto Sans SC pour le chinois simplifié, Noto Sans KR pour le coréen) et ajoutez son chemin à la liste `_JP_FONT_PATHS` dans `c_lord/discord_ui/table_renderer.py`. Les contributions ajoutant une détection de polices multilingue sont les bienvenues.

## Tâches planifiées

Une tâche planifiée est un prompt que c-lord exécute pour vous à intervalle fixe. c-lord ne gère que le *quand* ; tout ce qui relève du *quoi* — quoi vérifier, quoi publier, comment ne pas se répéter — se trouve dans le prompt. Les tâches sont stockées dans SQLite et sont ajoutées, modifiées et supprimées via l'[API REST](#api-rest) pendant que le bot tourne ; il n'y a donc ni modification de code ni redémarrage. La façon habituelle d'en créer une est de demander à Claude, dans un fil, de l'enregistrer ; Claude dispose de l'adresse de l'API dans `CLORD_API_URL` :

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

`name`, `prompt`, `interval_seconds` et `channel_id` sont tous obligatoires, et le salon doit être lié à un dépôt avec `/clord-init` ; `working_dir` et `run_immediately` sont facultatifs. `PATCH /api/tasks/{id}` avec `{"enabled": false}` met une tâche en pause sans la supprimer. Une boucle unique se réveille toutes les 30 secondes et lance tout ce qui est arrivé à échéance ; une tâche dont l'exécution précédente est encore en cours est ignorée, pas lancée en double.

Chaque exécution publie un message `🔄 **[Scheduled]** <task name>`, ouvre un fil dessus, crée une fenêtre tmux dans le `working_dir` de la tâche et y exécute Claude — la réponse arrive dans le fil comme pour n'importe quel autre tour. Ce à quoi doit ressembler une exécution planifiée (et ce qui compte comme un bug) est décrit dans [docs/specs/scheduled-tasks.md](../specs/scheduled-tasks.md).

## Automatisation GitHub et CI

Tout ce qui peut publier sur un webhook Discord peut confier du travail à Claude. Il existe deux façons de faire, selon qui écrit le prompt.

**L'expéditeur écrit le prompt.** Un message de webhook qui commence par `!clord`, dans un salon lié à un dépôt, ouvre une session exactement comme le ferait `/clord`. Détenir l'URL du webhook vaut permission ; traitez donc cette URL comme un secret.

**Le prompt est fixé de votre côté.** `WebhookTriggerCog` associe un préfixe de message à un prompt que vous avez défini dans le code. Le webhook peut seulement choisir *quel* déclencheur s'active ; il ne peut pas fournir de texte sur lequel Claude agirait. C'est le choix le plus sûr pour la CI. Ce cog n'est pas enregistré par défaut — ajoutez-le dans [votre propre bot](#utiliser-c-lord-comme-bibliothèque) :

```python
from c_lord import ClaudeConfig, WebhookTrigger, WebhookTriggerCog

docs_sync = WebhookTrigger(
    prompt="Read the latest commits on main and update the docs to match. Open a PR.",
    working_dir="/srv/checkouts/my-project",
    timeout=900,  # secondes
)

await bot.add_cog(
    WebhookTriggerCog(
        bot=bot,
        runner=ClaudeConfig(model="sonnet"),
        triggers={"🔄 docs-sync": docs_sync},      # préfixe de message → ce qu'il faut exécuter
        channel_ids={123456789012345678},          # n'écouter qu'ici
        allowed_webhook_ids={234567890123456789},  # uniquement ce webhook
    )
)
```

Un `WebhookTrigger` saute les vérifications de permission de Claude Code, sauf si vous passez `dangerously_skip_permissions=False`, car personne n'est là pour répondre à une demande — raison de plus pour restreindre `channel_ids` et `allowed_webhook_ids`.

**Ce qui se passe lors d'un déclenchement :** le message du webhook reçoit un fil, Claude s'exécute dans sa propre fenêtre tmux dans le `working_dir` du déclencheur (ou celui du runner), et sa réponse est publiée dans ce fil. Si la fenêtre ne peut pas être créée (par exemple parce que tmux n'est pas disponible sur l'hôte), l'exécution ne démarre pas : le fil explique pourquoi, le message du webhook reçoit ❌ et le propriétaire du bot est mentionné (#629).

### Exemple : synchronisation automatique de la documentation (à implémenter dans votre propre bot)

> **Remarque :** il s'agit d'un modèle que vous pouvez implémenter dans votre propre bot — c-lord n'embarque pas de workflow de synchronisation de documentation. Pour une implémentation de référence, voir [EbiBot](https://github.com/ebibibi/discord-bot).

À chaque push sur `main`, votre bot peut demander à Claude Code de :
1. Récupérer les dernières modifications et analyser le diff
2. Mettre à jour la documentation en anglais
3. Traduire en japonais (ou dans toute autre langue cible)
4. Créer une PR avec un résumé bilingue
5. Activer la fusion automatique — la PR est fusionnée automatiquement quand la CI passe

**GitHub Actions (à ajouter à votre dépôt) :**

```yaml
# .github/workflows/docs-sync.yml
on:
  push:
    branches: [main]
jobs:
  ask-claude:
    # Les commits de documentation de Claude portent [docs-sync] ; sans cette ligne, ils relanceraient le workflow
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

Côté bot, c'est le `WebhookTriggerCog` présenté plus haut, avec `🔄 docs-sync` comme déclencheur. Quand la PR de Claude est elle-même fusionnée dans `main`, ce push relancerait le workflow. La ligne `if:` interrompt la boucle, à condition que votre prompt demande à Claude de mettre `[docs-sync]` dans ses messages de commit.

### Être prévenu quand un tour se termine

Un script qui a confié du travail n'a pas besoin d'interroger le fil en boucle. Quand `CLORD_EVENT_LOG` ou `CLORD_EVENT_WEBHOOK_URL` est défini, c-lord émet un événement par tour terminé — `completion`, `blocked` ou `failure` — une fois la dernière réponse publiée ([spec](../specs/turn-end-events.md)).

### Mettre à jour le bot depuis un webhook

`AutoUpgradeCog` est destiné à un bot dans lequel c-lord est installé comme paquet. Comme pour `WebhookTriggerCog`, vous l'ajoutez vous-même :

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

Quand un message de webhook commençant par `trigger_prefix` (par défaut `🔄 upgrade`) arrive, le cog ouvre un fil et y rend compte de chaque étape :

1. Il exécute `uv lock --upgrade-package <package_name>` puis `uv sync` dans `working_dir` (remplacez l'une ou l'autre commande avec `upgrade_command` / `sync_command`).
2. Il note quels fils ont un tour en cours, puis attend la fin du travail en cours — chaque cog qui expose une propriété `active_count` (le protocole `DrainAware`) est interrogé, les vôtres compris. Après `drain_timeout` (300 s), il cesse d'attendre.
3. Il exécute `restart_command`. En son absence, rien n'est redémarré et le fil indique que la mise à jour est terminée.

Après le redémarrage, les fils notés à l'étape 2 reçoivent un avis discret d'une ligne indiquant que c-lord a redémarré. Claude n'est **pas** relancé par un nouveau prompt : les processus `claude` vivent dans tmux et n'ont jamais été arrêtés, il n'y a donc rien à reprendre (#406).

`upgrade_approval=True` demande confirmation avant l'étape 1, et `restart_approval=True` la demande une fois la nouvelle version installée, avant les étapes 2 et 3 ; dans les deux cas, on répond par une réaction ✅ dans le fil de mise à jour ou par le bouton Approve. `slash_command_enabled=True` ajoute `/upgrade`, qui lance le même pipeline sans webhook. Seuls les utilisateurs autorisés à piloter le bot peuvent l'exécuter. Elle fonctionne dans un salon comme dans un fil — lancée dans un fil, elle ouvre le fil de mise à jour dans le salon parent — et si une mise à jour est déjà en cours, elle vous le signale.

Un c-lord qui tourne depuis son propre clone n'a besoin de rien de tout cela : `git pull && systemctl --user restart c-lord.service`.

## API REST

Le plan de contrôle de c-lord pour les notifications, les tâches planifiées et le lancement de
sessions. Il démarre à chaque exécution — il n'est pas lié à la façon dont les réponses sont
acheminées (#712/#543). Nécessite aiohttp :

```bash
uv add "c-lord[api]"
```

L'API écoute sur `CLORD_API_HOST:CLORD_API_PORT` (par défaut `127.0.0.1:8080`). Quand
`CLORD_API_PORT` n'est **pas défini**, un port déjà pris n'est pas un problème : l'API passe au
port libre suivant (8081, 8082, … puis n'importe quel port libre) et journalise où elle s'est
installée, de sorte que deux clones sur un même hôte obtiennent chacun une API sans rien avoir à
configurer (#258). Un port que vous **définissez** est utilisé tel quel : s'il est pris, le bot
journalise un WARNING et tourne sans l'API plutôt que d'échouer au démarrage. Une valeur qui
n'est pas faite de chiffres ASCII (`²`, un mot) est ignorée avec un WARNING qui nomme
`CLORD_API_PORT`, comme si la variable n'était pas définie ; les espaces autour des chiffres sont
ignorés (#1218). Un nombre supérieur à 65535 est un port que vous avez défini et qui ne peut pas
être utilisé : un WARNING nomme `CLORD_API_PORT` et le bot tourne sans l'API (#1237). De même,
une entrée de `CLORD_TRUSTED_BOT_IDS` qui n'est pas faite de chiffres ASCII, ou qui est plus
longue qu'un identifiant Discord (20 chiffres), est ignorée avec un WARNING au démarrage, et les
autres identifiants de la liste continuent de fonctionner. Cela inclut les chiffres pleine
chasse (`１２３`), qui étaient auparavant lus comme l'identifiant 123 — écrivez l'identifiant en
chiffres ASCII.

Dans chaque session, Claude apprend où se trouve l'API par la variable d'environnement
`CLORD_API_URL` — le port réellement utilisé, de sorte qu'il n'y a jamais besoin de la maintenir
à la main en accord avec `CLORD_API_PORT`. Ne définissez `CLORD_API_URL` vous-même que pour
diriger les sessions ailleurs (vers un reverse proxy, par exemple). Quand l'API ne tourne pas,
les sessions ne reçoivent aucun `CLORD_API_URL`.

**Elle ne sert que l'utilisateur Unix qui exécute le bot** (#457). `POST /api/spawn` démarre
une session Claude Code ; atteindre ce port équivaut donc à disposer d'un shell sous cet
utilisateur — et le loopback est une frontière *réseau*, pas une frontière d'*UID* : tous les
comptes d'un hôte multi-utilisateur peuvent se connecter à `127.0.0.1`. L'UID du pair est donc lu
dans `/proc/net/tcp` et comparé à celui du bot, `/api/health` compris ; tous les autres reçoivent
`403`. Il n'y a rien à configurer — vos propres appels continuent de fonctionner tels quels.
Pour laisser entrer un autre utilisateur ou un autre hôte, définissez `CLORD_API_SECRET` et
faites-leur envoyer `Authorization: Bearer …`. Voir [docs/SECURITY.md](../SECURITY.md#rest-api-exposure-712-457).

| Méthode | Chemin | Ce qu'il fait |
|---|---|---|
| GET | `/api/health` | Vérification que le service est en vie |
| POST | `/api/spawn` | Ouvre un nouveau fil et y démarre Claude avec `prompt` (`channel_id` et `thread_name` facultatifs). Répond dès que le fil existe |
| POST | `/api/threads/{thread_id}/messages` | Publie `content` dans un fil |
| POST | `/api/threads/{thread_id}/sleep` | Met en veille l'espace de travail de ce fil, comme le ferait la mise en veille pour inactivité |
| POST | `/api/notify` | Publie une notification (`message`, et en option `title`, `color`, `channel_id`) |
| POST | `/api/schedule` | Publie une notification plus tard (`message`, `scheduled_at` au format ISO 8601) |
| GET | `/api/scheduled` | Liste les notifications encore en attente |
| DELETE | `/api/scheduled/{id}` | En annule une |
| POST | `/api/tasks` | Enregistre une [tâche planifiée](#tâches-planifiées) |
| GET | `/api/tasks` | Liste les tâches |
| PATCH | `/api/tasks/{id}` | Modifie une tâche : `enabled`, `prompt`, `interval_seconds`, `working_dir` |
| DELETE | `/api/tasks/{id}` | Supprime une tâche |
| POST | `/api/mark-resume` | Enregistre un fil avant que vous ne redémarriez vous-même le bot, afin qu'il reçoive l'avis de redémarrage au prochain démarrage |
| GET | `/api/lounge` | Lit les messages récents de l'AI Lounge (les sessions ne sont pas informées de l'existence du lounge ; rien n'y est donc publié à moins que vous ne le mettiez en place — #758) |
| POST | `/api/lounge` | Publie un message dans l'AI Lounge (avec un `label` facultatif) — les sessions de c-lord ne l'appellent pas d'elles-mêmes (#758) |

Chaque champ, chaque code de statut et chaque cas limite est décrit dans [docs/COMMANDS.md](../COMMANDS.md#rest-api). Deux exemples, exécutés sous l'utilisateur Unix du bot lui-même, donc sans en-tête nécessaire :

```bash
# Démarrer une session depuis un script — la réponse contient l'identifiant du fil
curl -X POST http://127.0.0.1:8080/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review the newest open PR and summarise the risks", "thread_name": "PR review"}'

# Signaler au salon principal que quelque chose s'est produit
curl -X POST http://127.0.0.1:8080/api/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "CI", "message": "Nightly build is green"}'
```

## Utiliser c-lord comme bibliothèque

c-lord est un paquet Python ordinaire, et `c-lord start` n'est qu'un programme parmi d'autres construit dessus. Pour l'intégrer à un bot à vous — avec vos propres cogs à côté — installez-le et appelez `setup_bridge()` :

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
        # await bot.add_cog(YourCog(bot))   ← vos propres cogs se placent ici
        await bot.start(os.environ["DISCORD_BOT_TOKEN"])


asyncio.run(main())
```

`setup_bridge()` enregistre les cogs standard de c-lord — neuf avec l'appel ci-dessus : le chat, la liaison salon-dépôt, les skills, les commandes d'espace de travail, le nettoyage des 30 jours, le planificateur, le miroir du transcript, le relais de connexion et `/version`. C'est voulu : une fonctionnalité standard ajoutée plus tard à c-lord arrive dans votre bot quand vous mettez à jour le paquet, sans modification de votre code. `WebhookTriggerCog` et `AutoUpgradeCog` n'en font pas partie ; ajoutez ces deux-là vous-même avec `bot.add_cog()`, comme montré plus haut.

```bash
uv lock --upgrade-package c-lord && uv sync
```

Utilisez `ClaudeDiscordBot` comme classe de bot — les cogs de c-lord lisent des éléments qu'elle porte, comme le salon principal. Le point d'entrée autonome, [`c_lord/main.py`](../../c_lord/main.py), sert de référence pour le reste : lecture de `.env`, verrous d'instance unique, démarrage de l'API REST et arrêt sur réception d'un signal.

## Architecture

### Correspondance entre Discord et tmux

L'idée « fil Discord = session Claude Code » repose sur une organisation tmux. Savoir où vit réellement un fil facilite beaucoup le débogage et l'intégration (vous pouvez faire `tmux attach` pour observer une session en direct) :

| Unité | tmux | Correspondance |
|------|--------------|---------|
| Dépôt auquel un fil est lié | session tmux | 1:1 |
| Fil | fenêtre tmux | 1:1 |

- **Les sessions ne naissent que dans un salon.** `/clord` ouvre un *nouveau* fil ; à l'intérieur d'un fil, la commande poursuit la session de ce fil, propose de reconnecter une session dont l'enregistrement a été nettoyé, ou refuse si le fil n'a jamais appartenu à c-lord (#551). Un fil de conversation ordinaire ne peut pas être transformé en session Claude — aucune commande n'en adopte un, et c'est voulu. Voir [docs/COMMANDS.md](../COMMANDS.md#chat--sessions).
- **1 dépôt = 1 session tmux — et non 1 salon.** Le nom de la session vient du dépôt auquel un fil est lié : le sien quand il en a un (`/clord-thread-init`, ou `/clord repo:<url>`), sinon celui du salon (`/clord-init`) ; les salons non liés se rabattent sur la session par défaut `clord`. Un salon dont les fils sont liés à des dépôts différents est donc réparti sur plusieurs sessions, et deux salons liés au même dépôt en partagent une. L'endroit où vit un fil donné — et la raison de ce choix — est détaillé dans [docs/specs/tmux-layout.md](../specs/tmux-layout.md).
- **1 fil = 1 fenêtre tmux = 1 session Claude Code.** Chaque fil reçoit sa propre fenêtre (`w1`, `w2`, …) qui exécute une session Claude Code. Vos échanges dans un fil *sont* donc les échanges avec cette session Claude — les réponses la poursuivent via `--resume`. Les numéros de fenêtre sont uniques **au sein d'une session**, pas au sein d'un salon.

Pour le « pourquoi » de cette conception, voir [docs/PHILOSOPHY.md](../PHILOSOPHY.md).

### Comment une réponse arrive dans Discord

```text
vous, dans un fil Discord                     Claude Code, dans la fenêtre tmux de ce fil
        │                                                    │
        │  votre message est saisi dans le panneau  ────────▶│
        │                                                    │  travaille : lit, modifie, lance des outils
        │                                                    ▼
        │                          ~/.claude/projects/<dir>/<session>.jsonl
        │                          (le transcript que Claude Code écrit de lui-même)
        │                                                    │
        ◀── c-lord suit ce fichier et publie ce qu'il dit ───┘
```

c-lord ne lit pas la réponse de Claude sur le terminal, et il ne demande pas à Claude de la publier. Il lit le transcript que Claude Code tient déjà et le retransmet. La capture de l'écran laissait sans cesse fuir des morceaux de l'interface du terminal dans Discord ; demander à Claude de publier faisait disparaître un tour entier chaque fois qu'il oubliait. Le transcript n'a aucun de ces deux problèmes, et c'est le seul chemin qu'emprunte une réponse.

Le panneau reste surveillé, mais pas pour son texte : pour remarquer qu'un tour s'est terminé, pour transformer en boutons Discord un menu qu'affiche Claude Code (`AskUserQuestion`, approbation d'un plan), pour accepter les demandes de permission de Claude Code et pour signaler une invite que c-lord ne reconnaît pas. [docs/ARCHITECTURE.md](../ARCHITECTURE.md) parcourt tout cela en détail, et [docs/DESIGN_DECISIONS.md](../DESIGN_DECISIONS.md) en consigne les raisons.

### Où se trouve quoi

```text
c_lord/
  cli.py            la commande `c-lord` : setup, start, version
  main.py           point d'entrée autonome — lit .env, prend les verrous, démarre tout
  setup.py          setup_bridge() : enregistre les cogs standard en un seul appel
  bot.py            ClaudeDiscordBot
  tmux.py           sessions et fenêtres tmux ; démarrage de claude, saisie dans le panneau
  session_dir.py    un git clone par fil
  claude/           exécution de chaque tour dans le panneau tmux, réglages du CLI, usage du contexte
  transcript/       suivi du transcript de Claude Code et mise en forme pour Discord ; journal des sous-agents
  cogs/             chat, liaison salon↔dépôt, skills, commandes d'espace de travail, planificateur,
                    déclencheurs webhook, mise à jour automatique, miroir du transcript
  discord_ui/       voyant d'état, boutons de question, ligne de progression du tour, images de tableaux et du panneau
  database/         SQLite : sessions, liaisons, tâches planifiées, notifications, menus en attente
  ext/api_server.py API REST
  skills/           le skill discord-read placé dans chaque session
deploy/             l'unité systemd
scripts/            install-systemd.sh, staging.sh, outils de preuves et de release
docs/               guides, et docs/specs/ — comment chaque fonctionnalité doit se comporter
```

### Ce que c-lord est, et ce qu'il n'est pas

- **Une passerelle, pas une IA.** c-lord n'a ni prompts, ni outils, ni mémoire qui lui soient propres. Tout cela appartient à Claude Code, et tout ce que vous avez configuré pour Claude Code — `CLAUDE.md`, skills, serveurs MCP, hooks — fonctionne dans un fil, parce que c'est le même programme.
- **Le vrai CLI, pas l'API.** Chaque fil exécute le `claude` interactif que vous lanceriez dans un terminal. c-lord n'appelle jamais l'API d'Anthropic.
- **tmux reste.** c-lord ne remplace pas tmux ; il vous permet de gérer tmux depuis Discord. Le panneau est toujours là pour que vous puissiez vous y attacher.
- **Des sessions qui survivent aux connexions.** Claude tourne sur le serveur. Que votre ordinateur portable se mette en veille, que votre train entre dans un tunnel ou que le bot redémarre, cela ne l'arrête pas.
- **Vous jugez depuis Discord ; Discord doit donc en montrer assez pour juger.** La progression, les résultats, les fichiers et les échecs arrivent tous dans le fil, et un échec dit ce qui a échoué et quoi faire ensuite au lieu de rester muet.
- **Rien à configurer dans le cas courant.** Les nouvelles fonctionnalités sont activées par défaut avec des réglages raisonnables, et mettre à jour le paquet est tout ce qu'un utilisateur du paquet a à faire.

Le raisonnement qui les sous-tend se trouve dans [docs/PHILOSOPHY.md](../PHILOSOPHY.md) (en japonais).

## Sécurité

Parler à c-lord, c'est exécuter des commandes sur la machine où il vit. Les réglages par défaut partent de ce principe.

- **Autorisation des utilisateurs** — `allowed_user_ids` / `CLORD_ALLOWED_ROLE` restreignent qui peut invoquer Claude. **Si aucun des deux n'est configuré, seul le propriétaire de l'application Discord peut utiliser le bot** — parler à c-lord exécute des commandes shell sur son hôte ; « non configuré » ne veut donc pas dire « tout le monde » (#713). `CLORD_ALLOW_ANYONE=1` l'ouvre délibérément à tout le serveur (et le signale au démarrage). Voir [docs/specs/authorization-default.md](../specs/authorization-default.md)
- **L'API REST ne répond qu'à l'utilisateur Unix du bot lui-même**, sauf si vous définissez `CLORD_API_SECRET` (voir [API REST](#api-rest)).
- **Aucun shell entre les deux.** Chaque processus que c-lord démarre l'est avec une liste d'arguments (`asyncio.create_subprocess_exec`), jamais par l'intermédiaire d'un shell, et un prompt est passé après un `--` afin qu'il ne puisse pas être lu comme un flag.
- **Vérifié avant utilisation.** Un identifiant de session doit en avoir la forme avant d'atteindre `--resume` ; les noms de skills et les URL de dépôts sont validés de la même façon.
- **Le token du bot ne fuit pas dans les sessions par héritage.** Il est retiré de l'environnement de tmux et de l'environnement de la ligne de commande `claude`. Cela évite les accidents, mais n'arrête pas une session déterminée : `.env` est un fichier que le même utilisateur Unix peut lire.

[docs/SECURITY.md](../SECURITY.md) présente le modèle de menace et les risques acceptés en connaissance de cause.

## Développement

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup                                # une fois par clone : active le hook pre-commit

uv run pytest tests/ -v --cov=c_lord
uv run ruff check c_lord/ && uv run ruff format --check c_lord/
uv run pyright c_lord/
```

La CI exécute ces vérifications sur Python 3.10, 3.11 et 3.12. Elles utilisent des mocks ; une CI au vert ne montre donc pas qu'une modification fonctionne face à un vrai Discord et à un vrai tmux — c'est pourquoi une modification doit aussi être montrée en fonctionnement sur un bot de staging avant d'être fusionnée. [CONTRIBUTING.md](../../CONTRIBUTING.md) explique ce qu'une pull request doit contenir ; [docs/STAGING.md](../STAGING.md) explique les bots de staging.

## Comment ce projet est construit

**L'intégralité de ce code base a été écrite par [Claude Code](https://docs.anthropic.com/en/docs/claude-code)**, l'agent de codage IA d'Anthropic. L'auteur humain ([@ebibibi](https://github.com/ebibibi)) a fourni les exigences et la direction en langage naturel, mais n'a pas lu ni édité manuellement le code source.

Le paragraphe ci-dessus décrit le projet amont, claude-code-discord-bridge, qui a fourni à c-lord son architecture initiale sous licence MIT. Ce projet dérivé est désormais maintenu par [@yousan](https://github.com/yousan), qui définit les exigences, relit les pull requests et approuve toutes les modifications, tandis que Claude Code se charge de l'implémentation.

Au quotidien, cela se passe ainsi. Une modification commence par une issue assortie de critères d'acceptation. Claude Code — qui tourne le plus souvent dans un fil c-lord, si bien que c-lord est construit avec c-lord — écrit un test qui échoue, le fait passer, montre la modification en fonctionnement sur un bot de staging avec des captures d'écran, puis ouvre une pull request. Deux vérifications obligatoires protègent `main`, et une pull request n'est fusionnée que lorsque chaque point de la [Definition of Done](../../CLAUDE.md#definition-of-done-dod--single-source-of-truth) est satisfait.

L'historique propre à c-lord commence le 2026-02-22 par une copie de ce projet amont. Les rapports de bugs et les pull requests sont les bienvenus, qu'ils viennent de personnes ou d'agents.

## Exemple Concret

**[EbiBot](https://github.com/ebibibi/discord-bot)** — Un bot Discord personnel construit sur ce framework. Inclut la synchronisation automatique de documentation (anglais + japonais), les notifications push, le watchdog Todoist, les vérifications de santé planifiées et le CI/CD avec GitHub Actions. Utilisez-le comme référence pour construire votre propre bot.

EbiBot est le bot personnel d'ebibibi, construit sur le projet amont claude-code-discord-bridge — et non sur c-lord.

## Inspiré par

- [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge) de [@ebibibi](https://github.com/ebibibi) — La base amont sur laquelle repose ce projet.
- [OpenClaw](https://github.com/openclaw/openclaw) — Réactions emoji de statut, debounce de messages, découpage avec connaissance des blocs
- [claude-code-discord-bot](https://github.com/timoconnellaus/claude-code-discord-bot) — Approche d'invocation CLI + stream-json
- [claude-code-discord](https://github.com/zebbern/claude-code-discord) — Schémas de contrôle des permissions
- [claude-sandbox-bot](https://github.com/RhysSullivan/claude-sandbox-bot) — Modèle de fil par conversation

## Licence

[MIT](../../LICENSE)
