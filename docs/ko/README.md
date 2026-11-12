# c-lord

[![CI](https://github.com/yousan/c-lord/actions/workflows/ci.yml/badge.svg)](https://github.com/yousan/c-lord/actions/workflows/ci.yml)
[![Python 3.10 – 3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB.svg)](../../pyproject.toml)
[![MIT license](https://img.shields.io/badge/license-MIT-green.svg)](../../LICENSE)

**Discord에서 여러 Claude Code 세션을 병렬로 실행합니다. 스레드마다 별도의 git 클론에서 작업하므로 세션끼리 작업 트리를 공유하는 일이 없습니다.**

c-lord는 Discord 서버를, 내 컴퓨터에서 돌아가는 Claude Code 세션들의 관제실로 만들어 줍니다. 작업마다 스레드를 하나씩 맡겨 두고 자리를 떠나도 됩니다. 휴대폰, 노트북, 터미널 어디서든 전체 상황을 확인할 수 있고, 작업이 중간에 끊기지 않으며, 일어난 일은 모두 스레드에 남습니다.

<p align="center">
  <img src="../assets/demo-mock.svg" alt="목업: Claude Code 스레드 네 개가 있는 Discord 채널. 그중 열려 있는 스레드에는 요청, 터미널에서 입력한 메시지, 파일이 첨부된 답변이 보이고, 같은 세션에 터미널이 연결되어 있습니다(이름은 모두 가상입니다)" width="760">
</p>

## c-lord를 쓰는 이유

- 🎛️ **Discord가 여러 Claude Code 세션의 관제실이 됩니다.** 스레드 하나는 내 컴퓨터의 tmux에서 돌아가는 실제 대화형 Claude Code 하나입니다. 터미널에서 쓰는 것과 같은 프로그램입니다. 스레드에 파일을 올리면 Claude에게 전달되고, Claude가 만든 파일은 첨부 파일로 돌아옵니다. 키보드로 직접 다루고 싶다면 바로 그 세션에 `tmux attach` 하면 됩니다.
- ⏳ **시간 제한이 없습니다.** 오래 걸린다는 이유로 작업이 중단되지 않습니다. c-lord가 턴을 포기하는 것은 Claude의 화면이 5분 동안 멈춰 있을 때뿐이고, 그때도 Claude를 종료하지는 않습니다.
- 🧪 **스레드마다 완전히 분리된 워크스페이스.** 각 스레드는 저장소의 자체 클론에서 작업하므로, 다른 스레드를 건드리지 않고 자기만의 백엔드와 프런트엔드를 함께 띄우고 자기만의 `docker compose` 스택을 실행할 수 있습니다. c-lord는 어느 컨테이너가 어느 스레드의 것인지 알고 있으며, 스레드를 중지할 때 함께 멈춥니다. (`/clord-init`으로 채널을 저장소에 연결하면 켜집니다. 호스트 포트는 여전히 공유되므로 스택마다 다른 포트를 지정하세요.)
- 📜 **모든 것이 Discord에 남습니다.** 답변, 도구 사용 내역, 파일은 물론 tmux 페인에 직접 입력한 내용까지 스레드에 기록됩니다. 팀원들이 진행 상황을 따라갈 수 있고, 다른 AI 에이전트가 기록을 읽고 작업을 이어받을 수도 있습니다.
- 🔒 **누가 조작할 수 있는지는 직접 정합니다.** 기본 상태에서는 Discord 앱의 소유자만 사용할 수 있습니다(c-lord에 말을 거는 것은 내 컴퓨터에서 명령을 실행하는 것이기 때문입니다). 필요하면 특정 역할이나 지정한 사람들에게 열어 줄 수 있습니다.

**시작하기:** [빠른 시작](#빠른-시작) · [동작 방식](#핵심-아이디어-병렬-세션-각자-자기-클론에서) · [명령어](../COMMANDS.md) · [설정](#설정) · [아키텍처](#아키텍처)

[English](../../README.md) · [日本語](../ja/README.md) · [简体中文](../zh-CN/README.md) · 한국어 · [Español](../es/README.md) · [Português](../pt-BR/README.md) · [Français](../fr/README.md)

이 페이지는 [영어 README](../../README.md)를 한국어로 옮긴 것입니다. 두 문서의 내용이 서로 다르면 영어 README가 맞습니다.

> **[@ebibibi](https://github.com/ebibibi)(Masahiko Ebisuda)의 [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge)를 기반으로 합니다.** 이 프로젝트는 원래 ebibibi가 만든 것이며, 여기서는 MIT 라이선스에 따라 사용하고 있습니다. 훌륭한 토대를 만들어 주셔서 감사합니다.

> c-lord는 독립적인 오픈 소스 프로젝트입니다. Anthropic이 만들거나 후원하거나 보증하는 것이 아니며, "Claude"와 "Claude Code"는 Anthropic의 상표입니다. c-lord는 사용자가 직접 설치하고 로그인한 Claude Code CLI를 구동할 뿐입니다.

**이 페이지의 내용:** [동작 방식](#핵심-아이디어-병렬-세션-각자-자기-클론에서) · [빠른 시작](#빠른-시작) · [스레드에서 작업하기](#스레드에서-작업하기) · [워크스페이스](#워크스페이스) · [명령어](#명령어) · [설정](#설정) · [예약 작업](#예약-작업) · [GitHub 및 CI](#github-및-ci-자동화) · [REST API](#rest-api) · [라이브러리로 사용](#c-lord를-라이브러리로-사용하기) · [아키텍처](#아키텍처) · [보안](#보안) · [개발](#개발) · [만들어지는 방식](#이-프로젝트가-만들어지는-방식)

## 핵심 아이디어: 병렬 세션, 각자 자기 클론에서

Discord 스레드 하나하나가 독립된 Claude Code 세션이며, 서버의 자체 tmux 창에서 실행됩니다. 여러 스레드에 작업을 보내면 나란히 실행되고, 노트북을 덮어도 계속 돌아가며, 스레드에 답글을 달면 멈춘 지점부터 이어집니다.

병렬 세션이 서로의 작업을 망가뜨리지 않게 해 주는 것은 **격리**입니다. 채널이 저장소에 연결되어 있으면(`/clord-init`) 모든 스레드가 각자 독립된 `git clone`에서 작업합니다. 세션끼리 작업 트리를 공유하지 않으므로, 한 스레드의 커밋되지 않은 변경이 다른 스레드에 나타날 수 없습니다. 세션들은 사람 기여자들과 마찬가지로 git 원격 저장소, 즉 브랜치·푸시·PR을 통해서만 만납니다.

```text
스레드 A (기능 개발)  ──→  Claude Code  (자체 클론)  ─┐
스레드 B (PR 리뷰)    ──→  Claude Code  (자체 클론)   ├─→  git 원격 저장소 (브랜치 · PR)
스레드 C (문서)       ──→  Claude Code  (자체 클론)  ─┘
```

**현재 세션들은 서로 조율하지 않습니다.** 이 README의 이전 버전은 네 가지 조율 메커니즘을 설명했는데, 실제 상태는 다음과 같습니다([#758](https://github.com/yousan/c-lord/issues/758)).

| 메커니즘 | 실제 상태 |
|---|---|
| **동시 실행 안내** (병렬 작업에 관해 Claude에게 주는 지시) | **구현되지 않음** — 세션은 tmux TUI(#53)에서 실행되는데, 여기에는 턴마다 시스템 프롬프트를 전달할 통로가 없어서 한 번도 전달된 적이 없습니다. c-lord는 이 안내를 더 이상 만들지 않습니다(#766) |
| **활성 세션 레지스트리** | 메모리에 존재하고 `/workspace-cleanup`이 사용하지만, **세션에는 보이지 않습니다**(세션에 보내 주는 것이 없습니다) |
| **조율 채널** | **기본적으로 꺼져 있습니다.** `COORDINATION_CHANNEL_ID`를 설정하면 세션의 턴이 끝날 때 한 줄짜리 알림을 게시합니다. 시작 이벤트는 없습니다 |
| **AI Lounge** | REST 엔드포인트는 동작하지만 **lounge가 있다는 사실을 Claude에게 알려 주는 것이 없어서** 실제로는 아무것도 게시되지 않습니다 — [AI Lounge](#ai-lounge-세션에는-전달되지-않음) 참고 |

정리하면, 제공되는 것은 분리된 작업 트리이지 실시간 조율이 아닙니다. 클론 바깥에 있는 것들, 예를 들어 같은 원격 브랜치, 공유 포트나 데이터베이스, 스테이징 봇은 여전히 충돌할 수 있습니다. 스레드마다 별도의 브랜치를 쓰세요.

### AI Lounge (세션에는 전달되지 않음)

> **상태: 연결되어 있지 않음**([#758](https://github.com/yousan/c-lord/issues/758)). lounge는 모든 세션에 일회성 시스템 컨텍스트로 주입되도록 설계되었습니다. 그런데 세션이 tmux TUI(#53)에서 실행되기 때문에 턴마다 그것을 전달할 통로가 없고, 그래서 c-lord는 이를 만들지 않습니다(#766). Claude는 lounge가 있다는 사실을 전달받지 못하며, 실제로는 아무것도 게시되지 않습니다.

REST 엔드포인트는 여전히 동작하므로 lounge를 직접 활용할 수는 있습니다. 예를 들어 저장소의 `CLAUDE.md`에 사용법을 적어 두는 방법이 있습니다.

```bash
# 메모 게시 (SQLite에 저장되며, lounge 채널에도 전달됩니다. 이 채널의 기본값은 COORDINATION_CHANNEL_ID입니다)
curl -X POST "http://localhost:8080/api/lounge" \
  -H "Content-Type: application/json" \
  -d '{"message": "Starting auth refactor on feature/oauth", "label": "feature dev"}'

# 최근 메모 읽기
curl "http://localhost:8080/api/lounge"
```

## 빠른 시작

항상 켜져 있는 Linux 컴퓨터(WSL2도 가능)가 필요하며, 다음이 설치되어 있어야 합니다.

- Python 3.10 이상과 [uv](https://docs.astral.sh/uv/)
- `git`과 `tmux`
- 이미 로그인된 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — 확인 삼아 `claude`를 직접 한 번 실행해 보세요
- 봇을 추가할 권한이 있는 Discord 서버

### 1단계 — Discord 봇 만들기

1. [Discord Developer Portal](https://discord.com/developers/applications)을 열고 **New Application**을 만듭니다.
2. **Bot** 페이지에서 (*Privileged Gateway Intents* 아래의) **Message Content Intent**를 켜고 봇 **토큰**을 복사합니다.
3. **OAuth2 → URL Generator**에서 스코프 `bot`과 `applications.commands`를 체크한 다음, 아래 봇 권한을 체크합니다.

   | 권한 | 용도 |
   |---|---|
   | View Channel (채널 보기) | 채널을 볼 수 있어야 합니다 (아래 참고 사항 확인) |
   | Send Messages (메시지 보내기), Send Messages in Threads (스레드에서 메시지 보내기) | 답변 |
   | Create Public Threads (공개 스레드 만들기) | 세션마다 스레드 하나 |
   | Read Message History (메시지 기록 보기) | 봇이 없는 동안 올라온 메시지 따라잡기 |
   | Add Reactions (반응 추가하기), Manage Messages (메시지 관리하기) | 내 메시지에 붙는 상태 램프와 그 램프 지우기 |
   | Manage Webhooks (웹후크 관리하기) *(선택)* | 서브에이전트의 메시지를 각자의 이름으로 표시 (`CLORD_SUBAGENT_VOICES=1`) |

4. 생성된 URL을 열어 봇을 서버에 추가합니다.

> **비공개 채널 / 권한이 잠긴 채널:** 채널이 `@everyone`에게 **View Channel**을 거부하고 있다면(비공개 채널), 위의 서버 단위 초대만으로는 *부족합니다*. Discord에서는 채널 단위의 거부가 서버 단위 권한보다 우선하기 때문입니다. 해당 채널의 권한 설정에 **봇의 역할**(또는 봇 멤버)을 추가하고, 최소한 **View Channel**, **Create Public Threads**, **Send Messages**, **Send Messages in Threads**를 허용해야 합니다. "Manage Threads"(스레드 관리하기)만 허용해서는 **소용이 없습니다**.
>
> 봇에게 채널에서 이 권한들이 없으면 **`/clord`가 허용해야 할 권한을 정확히 알려 줍니다(나에게만 보이는 답변으로)**(#443). 슬래시 명령어는 봇이 그 채널을 볼 수 있든 없든 봇에게 전달되기 때문입니다. 다음 단계로 넘어가기 전에 권한부터 고치세요. **View Channel**이 없으면 봇은 그 채널의 메시지를 전혀 받지 못하므로, 그곳의 스레드에 쓴 내용이 Claude에게 닿지 않습니다.

### 2단계 — 설정 마법사 실행하기

마법사가 `.env`를 대신 작성해 주므로 직접 편집할 것은 없습니다.

```bash
# GitHub에서 바로 실행 (아무것도 설치하지 않음):
uvx --from "git+https://github.com/yousan/c-lord.git" c-lord setup

# 또는 클론에서 실행 (서비스로 실행하려면 이쪽이 필요합니다):
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv run c-lord setup
```

마법사는 다음 여섯 가지를 이 순서대로 묻습니다.

1. **Claude Code CLI** — `claude`를 실행할 수 있는지만 확인하고, 실행할 수 없으면 알려 줍니다.
2. **봇 토큰** — 토큰은 Discord API로 검증됩니다(`Bot xxx` 형태로 붙여 넣은 토큰은 `xxx`로 저장됩니다. 비어 있거나 잘못된 답이 3번 이어지면 `.env`를 쓰지 않고 종료합니다).
3. **채널** — 봇이 볼 수 있는 텍스트 채널 목록을 보여 주므로 번호로 고르면 됩니다(ID를 복사할 필요가 없습니다). 숫자로 된 채널 ID를 붙여 넣어도 됩니다(채널 URL이나 `²` 같은 숫자를 입력하면 다시 묻습니다). 이 값이 봇의 홈 채널인 `DISCORD_CHANNEL_ID`가 됩니다.
4. **내 Discord 사용자 ID** — 선택 사항이며, @멘션을 받을 사람입니다. 숫자 ID 대신 사용자 이름을 입력하면 다시 묻습니다. `c-lord start`가 그런 값을 거부하기 때문입니다.
5. **작업 디렉터리** — 자체 클론이 없는 실행에 쓰이는 기본 디렉터리입니다.
6. **모델** — `sonnet`, `opus` 또는 `haiku`.

그런 다음 `.env`를 작성하고(다른 위치에 두려면 `--env PATH`를 사용하세요), 그 자리에서 봇을 시작할지 묻습니다.

### 시작 / 중지

```bash
c-lord start    # 봇을 시작합니다 (현재 디렉터리의 .env를 읽습니다)
c-lord start --env /path/to/.env   # .env 위치를 직접 지정
c-lord version  # 실행 중인 빌드를 출력합니다. 예: v1.4.183-bd80c47e-20260908
```

클론에서는 이 명령어들 앞에 `uv run`을 붙이세요. 이렇게 시작한 봇은 포그라운드에서 실행되며 Ctrl-C로 멈춥니다. 계속 실행해 두려면 [서비스로 실행하기](#서비스로-실행하기-systemd---user--재부팅-후에도-유지)를 참고하세요.

> **설정 우선순위 (#324):** `.env` 파일에 정의된 키는 상속받은 프로세스 환경 변수보다 항상 우선합니다. 즉 어느 디렉터리에서 시작하느냐가 봇의 정체를 결정합니다. 파일에 *없는* 키는 여전히 프로세스 환경 변수에서 가져오므로, 환경 변수만으로 구성한 환경도 그대로 동작합니다.

### 3단계 — 저장소를 연결하고 세션 시작하기

세션은 슬래시 명령어로 시작하며, 채널 자체에 일반 메시지를 입력해서는 시작되지 않습니다(일반 메시지는 무시되므로 채널을 평소처럼 대화하는 곳으로 둘 수 있습니다).

```text
/clord-init repo:https://github.com/you/your-project.git     ← 채널마다 한 번
/clord Fix the flaky login test                               ← 스레드를 열고 그 안에서 Claude를 시작합니다
```

`/clord-init`은 채널이 어느 저장소를 대상으로 작업하는지 기록합니다. 기본적으로 Discord는 이 명령어를 **Manage Server**(서버 관리하기) 권한이 있는 멤버에게만 보여 줍니다. 그 뒤로는 `/clord`를 실행할 때마다 새 스레드가 열리고, 그 스레드용으로 저장소를 클론한 다음, 클론 안에서 Claude Code가 시작됩니다. **그 스레드 안에** 쓰는 모든 내용은 같은 Claude 세션으로 전달됩니다. 한 스레드에서만 다른 저장소를 쓰려면 저장소를 지정하세요: `/clord repo:<url> <prompt>`. 이 방법은 연결된 저장소가 없는 채널에서도 동작합니다.

호스트는 자신의 git 자격 증명으로 클론하므로, 비공개 저장소를 쓰려면 호스트에 SSH 키나 credential helper가 설정되어 있어야 합니다. 클론에 실패하면 스레드에 git의 오류 메시지 그대로와 확인할 사항이 표시됩니다([사양](../specs/workspace-setup-failure.md)).

### 서비스로 실행하기 (systemd --user — 재부팅 후에도 유지)

c-lord를 백그라운드에서 계속 실행하고 **호스트가 재부팅될 때 자동으로 시작되게** 하려면 `systemd --user` 서비스로 설치하세요. 클론에서 다음을 실행합니다.

```bash
bash scripts/install-systemd.sh
```

이 스크립트는 [`deploy/c-lord.service`](../../deploy/c-lord.service)를 그대로 `~/.config/systemd/user/c-lord.service`로 설치하고, 호스트마다 다른 값(이 클론의 경로, 사용 중인 `uv`, `PATH`)을 드롭인 `c-lord.service.d/10-install.conf`에 기록하고, 서비스를 활성화하고, **linger**를 켜서 아무도 로그인하지 않은 상태에서도 부팅 시 서비스가 시작되게 합니다. `deploy/c-lord.service`가 바뀌면 다시 실행하세요. 그 뒤로는 표준 systemd 명령어로 관리합니다.

```bash
systemctl --user restart c-lord.service     # `git pull` 후
systemctl --user stop    c-lord.service
systemctl --user status  c-lord.service
journalctl --user -u c-lord.service -f      # 실시간 로그
```

유닛을 설치한 뒤에는 **이 클론의 봇을 시작하는 것은 systemd뿐이어야 합니다.** 봇을 kill한 뒤 손으로 다시 시작하지 마세요. systemd가 봇을 다시 띄우려다 단일 인스턴스 잠금에 걸려 포기하고(`Start request repeated too quickly`), 봇은 감시받지 않는 상태로 계속 돌게 됩니다. `bash scripts/staging.sh restart|stop`은 이 점을 알고 있습니다. 사용자 유닛의 `WorkingDirectory=`가 가리키는 클론에서는 kill하는 대신 `systemctl --user restart|stop`을 호출하고, `systemctl --user`를 쓸 수 없으면 (kill하지 않고) 오류로 끝납니다. `bash scripts/staging.sh status`는 각 봇 프로세스가 유닛의 cgroup 안에 있는지 보여 줍니다.

시스템 서비스가 아니라 `--user`인 이유: c-lord는 사용자의 `tmux`, `~/.claude` 세션 파일, `uv`, `claude` CLI를 구동하므로 **사용자 본인**의 환경에서 본인 권한으로 실행하는 것이 가장 깔끔합니다. root/시스템 서비스라면 이 모든 것을 `User=`/`Environment=`로 다시 구성해야 합니다. 사용자 서비스가 로그아웃 후에도 살아 있고 부팅 시 시작되게 하는 것은 `loginctl enable-linger "$USER"`이며, 설치 스크립트가 대신 실행해 줍니다.

> **수동 설치:** [`deploy/c-lord.service`](../../deploy/c-lord.service)를 복사하고 `CHANGE-ME` 경로 두 곳(클론 디렉터리와 `uv`)을 고친 다음 `systemctl --user daemon-reload && systemctl --user enable --now c-lord.service && loginctl enable-linger "$USER"`를 실행합니다.

> **WSL 참고:** `systemctl --user`가 `Failed to connect to bus`를 출력한다면 사용자 D-Bus 세션이 올라와 있지 않은 것입니다. `/etc/wsl.conf`의 `[boot]` 아래에 `systemd=true`가 있는지 확인하고 WSL을 재시작하세요(`wsl --shutdown`). 또는 봇을 kill해서 `Restart=always`가 다시 띄우게 하는 방법으로 재시작할 수도 있습니다(클론이 여러 개일 때 `pkill -f c_lord.main`은 안전하지 않으므로 PID를 지정해 kill하세요).

## 스레드에서 작업하기

아래는 Discord에서 실제로 보게 되는 모습입니다. 각 항목에는 그 동작이 어떠해야 하는지를 정의한 페이지가 링크되어 있습니다. 전체 색인은 [docs/specs/](../specs/README.md)에, 일상적인 사용 안내는 [docs/USER_GUIDE.md](../USER_GUIDE.md)에 있습니다.

### Claude와 대화하기

- **스레드 하나가 대화 하나입니다.** 스레드에 답글을 달면 같은 Claude Code 세션이 컨텍스트를 그대로 유지한 채 이어집니다(`--resume`). 오늘이든, 내일이든, 봇이 재시작된 뒤든 마찬가지입니다.
- **새 메시지는 진행 중인 작업을 중단시킵니다.** Claude가 아직 작업 중일 때 다시 메시지를 쓰면 실행 중인 턴이 중단되고 새 지시가 실행됩니다. 먼저 `/stop`을 할 필요가 없습니다.
- **중지하기.** `/stop`, 또는 턴이 실행되는 동안 스레드 맨 아래에 있는 **⏹ Stop** 버튼은 턴을 멈추고 Claude가 실행해 둔 셸 명령도 함께 멈춥니다. 대화는 유지되며 다음 메시지로 이어 갈 수 있습니다([사양](../specs/stop-button.md)).
- **보내는 내용에 길이 제한이 없습니다.** 길게 붙여 넣은 내용도 통째로 전달됩니다. 메시지를 정말로 전달할 수 없을 때는 스레드에 그 사실이 표시됩니다. 빈 턴이 조용히 끝나 버리는 일은 없습니다([사양](../specs/input-delivery.md)).
- **파일 보내기.** 메시지 하나에 파일을 10개까지 첨부할 수 있습니다. 각 파일은 스레드의 클론 안(`.clord/attachments/…`, git에는 포함되지 않음)에 저장되고, Claude에게 그 경로가 전달됩니다([사양](../specs/attachments.md)).
- **파일 받기.** Claude가 건네는 파일은 원래 이름 그대로 스레드에 첨부됩니다. 기본 한도는 25 MB입니다([사양](../specs/user-file-delivery.md)).
- **긴 답변도 빠짐없이 도착합니다.** Discord의 2000자 제한을 넘는 답변은 코드 블록을 깨뜨리지 않고 여러 메시지로 나뉩니다([사양](../specs/long-reply-split.md)).
- **터미널도 그대로 쓸 수 있습니다.** 스레드의 창에 `tmux attach` 해서 입력하면 됩니다. 거기서 입력한 내용은 스레드에 내 메시지로 나타나고, Claude의 답변이 평소처럼 뒤따릅니다([사양](../specs/mirrored-events.md)).
- **봇이 내려가 있는 동안 올라온 메시지도 사라지지 않습니다.** 봇이 다시 연결되거나 재시작되면 그동안 자기 스레드에 도착한 메시지를 읽어 실행하고, 늦어진 이유를 한 줄로 알려 줍니다([사양](../specs/gateway-backfill.md)).
- **스레드 이름은 사용자의 것입니다** — c-lord가 스레드 이름을 제멋대로 다시 요약하는 일은 없습니다. 스레드는 열릴 때의 이름을 유지합니다(앞뒤로 `W<N> │` 접두사와 `#<issue>` 번호만 붙습니다). 최근 대화를 새로 요약한 이름이 필요하면 스레드에서 `/thread-rename`을 실행하세요(sonnet). `CLORD_AUTO_TOPIC=1`로 예전의 자동 요약을 되살릴 수 있습니다(#705)

### 상황 파악하기

- **실시간 상태** — 내 메시지에 붙는 이모지 반응 램프입니다. Claude가 작업하는 동안은 🟢 실행 중, 내 차례가 되면 🟡 대기 중입니다(오류 시 ❌, 10초 / 30초 동안 조용하면 ⏳ / ⚠️). 반응은 사용량이 많을 때도 빠르게 갱신됩니다. 스레드 이름에 붙는 🟢/🟡 램프는 기본적으로 꺼져 있습니다(#329). 느리고 결과적으로만 일치하는 이 사이드바 표시가 필요하면 `CLORD_THREAD_LAMP=1`을 설정하세요(#246)
- **도구 사용 내역** — 도구 호출마다 게시하지 않고 Claude Code 자체의 트랜스크립트에서 읽어 옵니다. 조용해진 턴에는 진행 상황 줄 하나가 표시되고(`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件` — "작업 중 5:56 · 도구 61건"이라는 뜻, 아래 *턴 진행 상황 줄* 참고), 그 턴의 도구 호출과 진행 문장은 `progress.txt`로 답변에 첨부됩니다(#723)
- **턴 진행 상황 줄** — 턴이 90초 동안 조용하면 작은 글씨의 줄 하나가 나타나(`⚙️ 作業中 5:56 · 🔧 Bash(…) · ツール 61 件`) 15초마다 그 자리에서 갱신되고, 실제 출력이 돌아오는 순간 사라집니다. 도구 활동마저 멈추면 `⏳ 待機中`("대기 중")이라고 표시합니다. Claude가 진행 문장(페인의 "●" 줄)을 쓰면 이 줄은 도구 대신 가장 최근 문장을 보여 줍니다(#883). 이 문장들도 `progress.txt`에 들어가며, 별도의 메시지로 게시되지는 않습니다. 턴 바깥에서는 게시되지 않습니다. `CLORD_TURN_PROGRESS=0`으로 끌 수 있습니다(#539)
- **컨텍스트 사용량** — 각 턴은 한 줄짜리 푸터로 끝납니다(`📊 7% context (69k/1.0M) · …`). 컨텍스트 윈도우 사용률(입력 + 캐시 토큰, 출력 제외), 모델, 버전, 비용이 표시됩니다. 83.5%를 넘으면, 즉 다음 턴에 자동 압축이 실행될 수 있는 수준이 되면 ⚠️ 경고로 바뀝니다(`docs/specs/context-footer.md`)
- **압축 알림** — Claude Code가 컨텍스트를 압축하면, 스레드에는 원본 이어가기 요약 대신 `🗜️ コンテキストを圧縮しました`("컨텍스트를 압축했습니다") 한 줄이 표시됩니다(#628)
- **완전 정지 램프** — 30초 동안 아무 활동이 없으면 내 메시지의 반응이 ⚠️로 바뀝니다(확장 사고나 컨텍스트 압축 중에도 이만큼 조용할 수 있습니다). 스레드에 메시지가 게시되지는 않습니다(#473)
- **턴 종료 알림** — 턴이 끝나면 스레드에서 그 턴을 요청한 사람을 @멘션하므로 알림을 받게 됩니다. 답변이 나오지 않은 턴은 끝났다고 하지 않고 답변이 없었다는 사실을 알려 줍니다([사양](../specs/turn-end-notification.md)).
- **타임아웃 알림** — 경과 시간과 재개 방법을 담은 임베드입니다. Claude가 정말로 꼼짝하지 못할 때만(페인이 해당 시간 내내 멈춰 있고 *동시에* 프롬프트에서 대기 중인 상태도 아닐 때) 표시됩니다. 정상적으로 끝난 턴에서는 표시되지 않고(#541), 사용자의 답을 기다리는 질문/계획 메뉴가 떠 있을 때도 표시되지 않습니다(#751)
- **사용 한도와 로그인** — 요금제의 사용 한도에 도달하면 그 사실 그대로, 한도가 초기화되는 시각과 함께 알려 줍니다([사양](../specs/usage-limit.md)). Claude Code의 로그인이 만료되면 소유자에게 호스트와 멈춘 스레드를 알려 주는 DM이 한 통 가고, 그 DM에서 다시 로그인할 수 있습니다([사양](../specs/login-alert.md)).
- **📊 Session Status 보드** — 기본적으로 꺼져 있습니다(#761). `CLORD_SESSION_STATUS_BOARD=1`을 설정하면 `DISCORD_CHANNEL_ID` 채널에 실시간 보드 하나를 유지하면서 각 세션을 🟢 Auto-processing / 🟡 Waiting for input으로 나열합니다. 꺼져 있는 동안에는 봇이 시작할 때 채널에 아무것도 게시하지 않고, 이전 실행이 남겨 둔 보드가 있으면 정리합니다. 스레드에 오는 턴 종료 알림 `🟡 Claude has finished … @you`는 영향을 받지 않습니다. `docs/specs/session-status-board.md` 참고

### Claude가 질문할 때

- **질문** — Claude가 `AskUserQuestion`을 호출하면 선택지가 스레드에 버튼이나 선택 메뉴로 나타나고, Claude는 사용자가 고른 것으로 작업을 계속합니다. 버튼은 봇이 재시작된 뒤에도 계속 동작합니다([사양](../specs/ask-menu-lifecycle.md)).
- **Plan Mode** — Claude가 `ExitPlanMode`를 호출하면 Claude Code가 보여 주는 승인 메뉴가 `AskUserQuestion`과 같은 방식으로 Discord에 연결됩니다. 선택지가 버튼으로 나타나고, Claude는 사용자가 고른 것으로 진행합니다(#251)
- **도구 권한 프롬프트** — Discord에는 표시되지 않습니다. c-lord가 페인에서 Claude Code의 권한 프롬프트를 수락하므로 턴이 계속 진행됩니다. Claude가 무엇을 할 수 있는지는 도구별 버튼이 아니라 세션이 실행되는 권한 모드와 설정으로 정해집니다

### 작업 나누기

- **서브에이전트 로그** — 한 스레드 안에서 서브에이전트들에게 나눠 맡긴 작업도 Discord에 남습니다. 실행마다 `🤖 サブエージェント「…」開始`("서브에이전트 「…」 시작") / `終了 · 6:12 · ツール 48 件`("종료 · 6:12 · 도구 48건") 줄이 게시되고, 종료 줄에는 `subagent-*.md`(전체 보고서, 맡긴 내용, 도구 호출)가 첨부되며, 시작 줄에 첨부된 사본은 실행되는 동안 갱신됩니다. `CLORD_SUBAGENT_VOICES=1`을 설정하면 각 에이전트의 메시지와 보고서가 에이전트 자신의 이름으로 추가됩니다(#971, `docs/specs/subagent-log.md`)

**새 스레드를 열어야 할 때와 한 스레드 안에서 작업을 나눠야 할 때** (#969): 새 스레드는 작업에 자체 브랜치나 자체 개발 서버, 또는 사용자와의 별도 대화가 필요할 때만 여세요. 조사, 리뷰, 분류처럼 하나의 작업을 나누려는 것이라면 스레드 안에서 서브에이전트를 쓰도록 Claude에게 요청하세요. 추가 `claude` 프로세스(각각 약 285 MB)가 뜨지 않으며, 각 서브에이전트의 시작, 종료, 전체 보고서도 스레드에 그대로 남습니다([서브에이전트 로그](../specs/subagent-log.md)). Agent Teams(`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`)는 실험적 기능이며, c-lord가 재시작하거나 세션을 재개하면 팀원 에이전트는 돌아오지 않습니다. 분할 페인 표시(`--teammate-mode tmux`)는 사용하지 마세요. c-lord가 구동하는 창에 페인을 추가하기 때문입니다.

### 스킬, 모델, 커밋

- **스킬** — `/skill`은 `~/.claude/skills/`에 있는 Claude Code 스킬 중 하나를 실행합니다. 이름은 자동 완성되고, 봇이 실행되는 동안 추가한 스킬도 1분 안에 나타납니다. 채널에서 실행하면 새 스레드가 열리고, 스레드에서 실행하면 그 세션을 이어 갑니다.
- **실행 중 모델 전환** — `/model`은 세션, 채널, 전역 범위에서 모델과 effort를 보여 주고 변경합니다(`include-existing`을 쓰면 열려 있는 세션도 다음 턴부터 바뀝니다)
- **누가 커밋을 요청했는지** — Claude가 스레드에서 만든 커밋에는 그 턴을 요청한 Discord 사용자와 Claude에 대한 `Co-authored-by:` 트레일러가 붙습니다. Claude에게 기억해 달라고 부탁하는 방식이 아니라 git 훅이 처리합니다. `CLORD_COAUTHOR=0`으로 끌 수 있습니다([사양](../specs/commit-coauthors.md)).

## 워크스페이스

**워크스페이스**는 한 스레드에 속한 모든 것, 즉 그 스레드의 Claude 세션, 클론, 그리고 그 클론에서 시작한 docker 환경을 가리킵니다.

- **스레드마다 git 클론 하나** — 채널이 저장소에 연결되어 있으면(`/clord-init`) 각 스레드는 자체의 독립된 `git clone`에서 작업합니다. 세션들이 공유하는 것은 git 원격 저장소뿐입니다
- **"끄기"에는 세 단계가 있고, 각 단계는 앞 단계를 포함합니다.** 어느 작업도 대화 기록이나 docker 볼륨을 삭제하지 않습니다([용어](../specs/workspace-vocabulary.md)).

  | | 멈추는 것 | 자동 | 수동 |
  |---|---|---|---|
  | **절전(Sleep)** | `claude` 프로세스 (docker는 계속 실행) | 4시간 유휴 후 (`CLORD_IDLE_SLEEP_HOURS`) | — 다음 메시지가 오면 아무 말 없이 깨어납니다 |
  | **중지(Stop)** | + docker 환경 | 7일 유휴 후 (`CLORD_IDLE_STOP_DAYS`) | `/workspace-stop`, `/workspace-start`로 되돌림 |
  | **삭제(Delete)** | + 클론 자체 | 30일 미사용 후 (아래 참고) | `/workspace-delete` |

- **깨어 있는 워크스페이스 수의 상한.** `CLORD_MAX_RESIDENT_WORKSPACES`(기본값은 호스트의 메모리로부터 계산)를 넘으면 가장 오래 유휴 상태였던 워크스페이스부터 절전 상태가 됩니다. 새 워크스페이스를 시작할 때 기다리게 되는 일은 없습니다([사양](../specs/resident-cap.md)).
- **채널 상태** — `/clord-status`는 이 채널의 세션들을 디렉터리 크기, `tmux attach` 대상, `claude --resume` 명령과 함께 나열합니다. `show_all`을 쓰면 닫힌 세션도 포함합니다(`docker ps -a` 방식). 예전의 `/sessions`, `/session-dirs`, `/resume-info`를 대체합니다.
- **워크스페이스 정리** — `/workspace-cleanup`은 실행 중인 세션이 사용하지 않는 클론 디렉터리를 회수합니다(`dry_run`으로 미리 확인 가능). 작업물(커밋되지 않은 변경, 푸시되지 않은 커밋, `.env` 같은 ignore된 파일)이 들어 있는 디렉터리는 절대 삭제하지 않으며, 어느 디렉터리가 그런지 답변에서 알려 줍니다(#1016)
- **알림을 남기는 30일 정리** — 시작할 때마다 **30일** 동안 사용되지 않은 워크스페이스를 정리합니다. 이 기간은 Claude Code 자체의 트랜스크립트 보존 기간(`cleanupPeriodDays`)을 따르므로, 그때쯤이면 Claude도 대개 그 대화를 잊은 상태입니다. 클론은 커밋되지 않은 작업이 없을 때만 삭제되고, 스레드와 세션을 연결하는 기록은 유지됩니다. 아직 열려 있는 스레드에는 무슨 일이 있었고 무엇이 남았는지 알리는 🧹 알림이 게시된 뒤 보관 처리됩니다. 이미 닫힌 스레드는 게시 없이 정리되므로 닫힌 상태로 남습니다. 나중에 그런 스레드에 글을 쓰면 디스크에 남아 있는 것에 다시 연결되거나, 같은 스레드에서 새 대화가 시작됩니다. 자세한 내용: [Why a thread loses its record](../COMMANDS.md#why-a-thread-loses-its-record-the-30-day-sweep).
- **클론들은 git 객체를 공유합니다.** 같은 저장소의 클론들은 하나의 로컬 미러에서 객체를 빌려 쓰므로, 새 스레드의 클론은 몇 초 만에 만들어지고 디스크도 거의 차지하지 않습니다. git을 쓰는 방법은 달라지지 않습니다. `CLORD_GIT_MIRRORS=0`으로 끌 수 있습니다([사양](../specs/git-object-mirrors.md)).

## 명령어

모든 명령어는 **[docs/COMMANDS.md](../COMMANDS.md)** 문서에 설명되어 있습니다. 가장 자주 쓰게 될 명령어는 다음과 같습니다.

| 명령어 | 사용 위치 | 하는 일 |
|---|---|---|
| `/clord-init repo:<url>` | 채널 | 채널을 저장소에 연결합니다 (인수 없이 실행하면 연결 상태를 보여 줍니다) |
| `/clord <prompt>` | 채널 | 스레드를 열고 세션을 시작합니다. `repo:<url>`로 그 스레드에 쓸 저장소를 고를 수 있습니다 |
| `/stop` | 스레드 | 실행 중인 턴을 멈춥니다. 대화는 유지됩니다 |
| `/clear` · `/compact` | 스레드 | 같은 스레드에서 처음부터 다시 시작 · 컨텍스트를 요약해 윈도우를 비움 |
| `/skill <name> [args]` | 채널 또는 스레드 | Claude Code 스킬을 실행합니다 |
| `/model` | 어디서나 | 세션, 채널 또는 전체의 모델과 effort를 보여 주거나 변경합니다 |
| `/clord-status` | 어디서나 | 이 채널의 세션들을 `tmux attach` 대상, `claude --resume` 명령과 함께 보여 줍니다 |
| `/workspace-stop` · `/workspace-start` · `/workspace-delete` · `/workspace-cleanup` | 스레드 / 어디서나 | [워크스페이스](#워크스페이스) 작업 |
| `/claude-restart` · `/resync` · `/tmux-screenshot` | 스레드 | Claude 프로세스 재시작 · 스레드를 페인에 다시 연결 · 페인의 이미지를 게시 |
| `/thread-rename` | 스레드 | 스레드 이름을 다시 요약합니다 |
| `/clord-reattach` | 스레드 | 정리된 스레드를 디스크에 남아 있는 것에 다시 연결합니다 |
| `/version` | 어디서나 | 어떤 빌드가 실행 중인지 보여 줍니다 |

대부분의 슬래시 명령어는 앞에 `!`를 붙인 텍스트 명령어로도 존재합니다(`!clord`, `!stop`, …). 웹후크가 보낼 수 있는 것은 이쪽입니다. Discord는 웹후크가 슬래시 명령어를 쓰도록 허용하지 않기 때문입니다. 스크립트나 다른 에이전트가 작업을 시작하는 방법도 이것입니다. 저장소에 연결된 채널에 `!clord`로 시작하는 웹후크 메시지를 게시하면 세션이 열립니다.

## 설정

모든 설정은 `.env`에서 합니다. 필수 항목은 처음 두 개뿐입니다. [.env.example](../../.env.example)은 주석이 달린 출발점일 뿐 전체 목록이 아닙니다. 아래 표에 나오는 변수 중 일부는 거기에 없고, 반대로 거기에는 여기에 싣지 않은 변수(테스트·모니터링 도구용)가 몇 개 들어 있습니다.

**연결**

| 변수 | 하는 일 | 기본값 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | 봇의 토큰 | (필수) |
| `DISCORD_CHANNEL_ID` | Claude와 대화할 채널의 ID. 여기에 나오는 모든 ID 설정(`DISCORD_OWNER_ID`, `COORDINATION_CHANNEL_ID`, `EXPECTED_BOT_USER_ID`)과 마찬가지로 ASCII 숫자만 쓸 수 있습니다. 링크, 이름, `²` 같은 값이 들어 있으면 `c-lord start`가 해당 설정의 이름과 값을 표시하고 멈춥니다(#1084). 숫자 앞뒤의 공백은 무시됩니다(#1194) | (필수) |
| `EXPECTED_BOT_USER_ID` | 봇 자신의 사용자 ID. 프로세스가 다른 봇으로 로그인하면 즉시 종료합니다. 한 호스트에서 여러 봇을 돌릴 때 설정해 둘 만합니다 | (꺼짐) |

**Claude Code**

| 변수 | 하는 일 | 기본값 |
|---|---|---|
| `CLAUDE_COMMAND` | 실행할 Claude Code 실행 파일 | `claude` |
| `CLAUDE_MODEL` | 새 세션에 쓸 모델. 실행 중에는 `/model`로 바꿉니다 | `sonnet` |
| `CLAUDE_EFFORT` | `--effort`로 전달되는 추론 effort (`low` / `medium` / `high` / `xhigh` / `max`). 설정하지 않으면 플래그를 전달하지 않습니다 | (CLI 기본값) |
| `CLAUDE_PERMISSION_MODE` | 세션이 실행되는 권한 모드 | `acceptEdits` |
| `CLAUDE_WORKING_DIR` | 자체 클론이 없는 실행(`working_dir`이 없는 예약 작업이나 웹후크 트리거)에 쓰이는 디렉터리 | 현재 디렉터리 |
| `CLORD_SESSION_PLUGINS` | `0`으로 설정하면, c-lord가 세션을 시작할 때 Discord 플러그인과 불필요한 language server 플러그인을 제외하던 동작을 하지 않습니다([사양](../specs/session-plugins.md)) | `true` |

**사용할 수 있는 사람**

| 변수 | 하는 일 | 기본값 |
|---|---|---|
| `DISCORD_OWNER_ID` | 봇을 조작할 수 있고, Claude가 입력을 필요로 할 때 @멘션을 받는 사용자의 ID. 설정하지 않으면 봇은 Discord 애플리케이션의 소유자만 허용합니다(#713) | (선택) |
| `CLORD_ALLOWED_ROLE` | 이 이름의 Discord 역할을 가진 멤버가 봇을 조작할 수 있습니다 (`DISCORD_OWNER_ID`와 OR 조건) | (선택) |
| `CLORD_ALLOW_ANYONE` | `1`로 설정하면 서버의 **모든** 멤버가 봇을 조작할 수 있습니다. 이는 호스트에서 셸 명령을 실행할 수 있다는 뜻입니다. 시작할 때 경고가 표시됩니다 | `false` |
| `CLORD_NOTIFY_DENIED` | `0`으로 설정하면, 허용되지 않은 사람이 봇 스레드에 글을 올렸을 때 애플리케이션 소유자에게 가는 DM을 보내지 않습니다 (글을 올린 사람에게는 아무것도 보이지 않으며, 거부 사실은 그래도 로그에 남습니다 — #346) | `true` |
| `CLORD_HOST_LABEL` | 로그인 장애 DM에서 이 컴퓨터를 부르는 이름 (#1309 — 두 번째 호스트와 구별하기 위한 것) | 호스트 이름 |
| `CLORD_LOGIN_RELAY` | `0`으로 설정하면 로그인 장애 DM은 그대로 보내되, 그 DM에서 다시 로그인하는 기능(로그인 링크 + 코드 답장, #1310)은 뺍니다 | `true` |

**한도와 워크스페이스**

| 변수 | 하는 일 | 기본값 |
|---|---|---|
| `MAX_CONCURRENT_SESSIONS` | **동시에 실행되는 턴**의 최대 수. 상주하는 `claude` 프로세스 수의 상한이 아닙니다. 세마포어는 턴 실행만 감싸며 턴이 끝나면 해제되지만, tmux 페인은 계속 살아 있습니다. 상주 상한은 `CLORD_MAX_RESIDENT_WORKSPACES`를 보세요(#576). **1 이상**의 정수여야 합니다. `0`, 음수, 숫자가 아닌 값이면 키 이름을 표시하고 시작을 멈춥니다(#1119). 18자리를 넘는 숫자는 키 이름이 담긴 WARNING과 함께 무시되고 기본값이 사용됩니다(#1256). | `3` |
| `CLORD_MAX_RESIDENT_WORKSPACES` | 살아 있는 `claude`를 동시에 가지고 있을 수 있는 워크스페이스의 최대 수. 상한을 넘으면 가장 오래 유휴 상태였던 것부터 절전 상태가 됩니다. 새 워크스페이스 생성이 막히는 일은 없습니다. 유휴 TTL과 달리 이 기본값은 호스트에 **따라 달라지므로**, 상수로 제공하지 않고 `MemTotal`(cgroup 제한이 있으면 그것을 우선)로부터 계산합니다. `0`이면 비활성화됩니다. `docs/specs/resident-cap.md` 참고. | 자동 (`max(2, MemTotal_GiB × 0.4 / 0.45)`) |
| `SESSION_TIMEOUT_SECONDS` | 세션 비활성 타임아웃. **1 이상**의 정수여야 하며, 그렇지 않으면 키 이름을 표시하고 시작을 멈춥니다(#1119). 18자리를 넘는 숫자는 키 이름이 담긴 WARNING과 함께 무시되고 기본값이 사용됩니다(#1256) | `300` |
| `CLORD_IDLE_SLEEP_HOURS` | 워크스페이스가 절전 상태가 되기까지의 유휴 시간(시간 단위, 소수 가능). `0`이면 비활성화됩니다 | `4` |
| `CLORD_IDLE_STOP_DAYS` | 워크스페이스가 중지되기까지의 유휴 일수. `0`이면 비활성화됩니다 | `7` |
| `CLORD_ORPHAN_SWEEP_DAYS` | 어떤 기록도 가리키지 않는, 변경 사항 없이 깨끗하고 손대지 않은 클론이 삭제되기까지의 일수. `0`이면 비활성화됩니다 | `30` |
| `SESSION_DIR_BASE` | 스레드별 클론이 놓이는 위치. 이미 쓰고 있는 것은 옮기지 마세요. `--resume`은 디렉터리 경로를 키로 삼습니다 | `~/.c-lord/<instance>/sessions` |
| `CLORD_INSTANCE` | 위의 `<instance>` | 클론의 디렉터리 이름 |
| `CLORD_GIT_MIRRORS` | `0`으로 설정하면 같은 저장소의 클론들이 git 객체를 공유하지 않습니다 | `true` |

**알림과 이벤트**

| 변수 | 하는 일 | 기본값 |
|---|---|---|
| `CLORD_OWNER_FALLBACK` | 사람이 요청하지 않은 턴(웹후크 / CI / 스케줄러)에 대해 소유자에게 대신 알리는 범위: `all`(턴 종료 🟡 + 일시 정지 + 실패), `blocked`(일시 정지 + 실패 — 잘 돌아갈 때는 조용하고 문제가 생기면 알림), `off`(실패를 포함해 전혀 알리지 않음) | `blocked` |
| `CLORD_EVENT_LOG` | 턴이 끝날 때마다(`completion` / `blocked` / `failure`) 마지막 답변이 게시된 뒤 이 파일에 JSON 한 줄을 추가합니다. 작업을 맡긴 에이전트나 CI가 스레드를 폴링하는 대신 `tail -F` 할 수 있게 하기 위한 것입니다. 서버 쪽 전용이며, [docs/specs/turn-end-events.md](../specs/turn-end-events.md)를 참고하세요(#917) | (꺼짐) |
| `CLORD_EVENT_WEBHOOK_URL` / `CLORD_EVENT_WEBHOOK_SECRET` | 같은 이벤트를 `X-Clord-Signature: sha256=<HMAC-SHA256 of the body>` 헤더와 함께 JSON으로 이 URL에 POST합니다. 시크릿이 없으면 아무것도 보내지 않습니다. 실패하면 몇 번 재시도하며, 턴을 막는 일은 없습니다(#917) | (꺼짐) |
| `CLORD_EVENT_DEFER_MAX_SECONDS` | 백그라운드 서브에이전트가 아직 실행 중일 때 `completion`을 보류해 두는 최대 시간. 이 시간이 지나면 그대로 보냅니다 | `3600` |
| `COORDINATION_CHANNEL_ID` | 세션의 턴이 끝날 때 한 줄짜리 알림을 받는 채널. AI Lounge의 기본 채널이기도 합니다. Discord id보다 긴(20자리) 숫자는 키 이름이 담긴 시작 시 WARNING과 함께, 설정되지 않은 것처럼 무시됩니다(#1256) | (선택) |

**서브에이전트**

| 변수 | 하는 일 | 기본값 |
|---|---|---|
| `CLORD_SUBAGENT_LOG` | `0`으로 설정하면 서브에이전트 로그를 끕니다. 서브에이전트 로그는 서브에이전트 실행마다 `🤖 … 開始` / `終了` 줄을 하나씩 게시하고, 종료 줄에는 `subagent-*.md`(전체 보고서, 맡긴 내용, 모든 도구 호출)를 첨부하며, 시작 줄에 첨부된 사본은 에이전트가 작업하는 동안 교체합니다. [docs/specs/subagent-log.md](../specs/subagent-log.md) 참고(#971) | `true` |
| `CLORD_SUBAGENT_VOICES` | `1`로 설정하면 부모가 맡긴 내용, 부모와 에이전트 사이의 모든 `SendMessage`, 각 최종 보고서도 **말한 쪽의 이름으로** 게시합니다. 누가 누구에게 무엇을 말했는지 스레드에서 따라갈 수 있습니다. 봇에게 Manage Webhooks 권한이 있으면 웹후크를 사용합니다(필수는 아닙니다. 없으면 이름이 본문 앞에 붙습니다). 도구 호출은 첨부 파일에만 남습니다(#971) | `false` |
| `CLORD_SUBAGENT_PROGRESS_SECONDS` | 실행 중인 서브에이전트에 첨부된 진행 상황 파일을 교체하는 주기 (내용이 바뀌었을 때만, 최솟값 10) | `60` |

**스레드에 표시되는 것**

| 변수 | 하는 일 | 기본값 |
|---|---|---|
| `CLORD_RENDER_TABLE_IMAGES` | `1`, `true` 또는 `yes`로 설정하면 GFM 파이프 표를 PNG 이미지로 렌더링해 Discord 메시지에 첨부합니다 | (선택) |
| `CLORD_SHOW_URL_EMBEDS` | `1`/`true`/`yes`/`on`으로 설정하면 Claude의 답변에 들어 있는 URL에 대해 Discord가 OGP/링크 미리보기 카드를 펼치게 합니다. 기본적으로 꺼져 있어 답변이 간결하게 유지됩니다(미리보기 카드 없음). | `false` |
| `CLORD_AUTO_TOPIC` | `1`/`true`/`yes`/`on`으로 설정하면 c-lord가 **새** 스레드의 첫 메시지에서 LLM(haiku)으로 스레드 이름을 요약합니다. #705 이전의 동작입니다. 기본적으로 꺼져 있어 스레드는 열릴 때의 이름을 유지하며, 필요할 때 `/thread-rename`(sonnet)으로 다시 요약합니다. | `false` |
| `CLORD_SESSION_STATUS_BOARD` | `1`/`true`/`yes`/`on`으로 설정하면 `DISCORD_CHANNEL_ID`에 📊 Session Status 보드를 표시합니다(#761). 기본적으로 꺼져 있어, 시작할 때 그 채널에 아무것도 게시하지 않고 이전 실행이 남긴 보드를 정리합니다. 스레드의 턴 종료 알림은 영향을 받지 않습니다. | `false` |
| `CLORD_THREAD_RETITLE` | `1`/`true`/`yes`/`on`으로 설정하면, 작업 내용이 바뀌었다고 LLM이 판단했을 때 대화 도중에 스레드 제목을 다시 붙입니다(#121). 기본적으로 꺼져 있습니다(#414). 스레드 이름을 너무 자주 바꿨기 때문입니다. | `false` |
| `CLORD_THREAD_LAMP` | `1`로 설정하면 스레드 이름에 🟢/🟡 램프를 붙입니다(사이드바 표시). Discord가 스레드 이름 변경을 강하게 제한하기 때문에 기본적으로 꺼져 있습니다. 내 메시지의 반응 램프는 항상 켜져 있습니다([사양](../specs/thread-lamp.md)) | `false` |
| `CLORD_TURN_PROGRESS` | `0`으로 설정하면 턴 진행 상황 줄을 끕니다. `CLORD_TURN_PROGRESS_QUIET_SECONDS`는 스레드가 얼마나 오래 조용해야 그 줄이 나타나는지를 바꿉니다 | `true` / `90` |
| `CLORD_COAUTHOR` | `0`으로 설정하면 Claude의 커밋에 `Co-authored-by:` 트레일러를 붙이지 않습니다 | `true` |

**프로세스와 tmux**

| 변수 | 하는 일 | 기본값 |
|---|---|---|
| `CLORD_TMUX_SCREENSHOT_ROWS` | `/tmux-screenshot`이 캡처하기 전에 창을 늘리는 높이(행 수). `0`이면 현재 크기 그대로 캡처합니다 | `100` |
| `CLORD_INSTANCE_ID` | 하나의 tmux 서버를 공유하는 c-lord 인스턴스들을 구별합니다. 두 인스턴스가 같은 디렉터리에서 시작할 때만 설정하세요 | 시작 디렉터리의 실제 경로 |
| `CLORD_SHUTDOWN_TIMEOUT_SECONDS` | SIGTERM/SIGINT를 받은 뒤 프로세스가 종료하는 데 쓸 수 있는 시간. 이 시간을 넘기면 스택을 로그에 덤프하고 상태 코드 70으로 종료합니다. `0`이면 비활성화됩니다 | `10` |

REST API에는 별도의 설정이 있습니다(`CLORD_API_HOST`, `CLORD_API_PORT`, `CLORD_API_URL`, `CLORD_API_SECRET`, `CLORD_API_ALLOW_ANY_PEER`). [REST API](#rest-api)를 참고하세요.

### URL 링크 미리보기 (CLORD_SHOW_URL_EMBEDS)

Claude의 답변에 URL(예: GitHub Issues 링크)이 들어 있으면, Discord는 보통 이를 세로로 긴 OGP 미리보기 카드(제목, 설명, 이미지)로 펼칩니다. 그러면 정작 답변은 아래로 밀리고 스레드가 어수선해집니다. c-lord는 기본적으로 이 미리보기를 **억제**하므로(각 답변에 `SUPPRESS_EMBEDS` 플래그를 설정합니다) 링크는 본문 안에 그대로 남고 답변을 깔끔하게 읽을 수 있습니다. Discord 기본 동작인 링크 미리보기 펼침을 되살리려면 `CLORD_SHOW_URL_EMBEDS=true`를 설정하세요.

### 표 이미지 렌더링 (CLORD_RENDER_TABLE_IMAGES)

이 기능을 켜면 Claude의 답변에 들어 있는 Markdown 파이프 표가 [Pillow](https://python-pillow.github.io/)로 PNG 이미지로 렌더링되어 텍스트와 함께 Discord 메시지에 첨부됩니다. 각 셀은 텍스트 구간과 이모지 구간으로 나뉘어, **텍스트는 CJK를 지원하는 글꼴로, 이모지는 컬러 이모지 글꼴로 풀 컬러로 그려집니다.** 그래서 🟢/🔴 상태 램프의 색이 유지됩니다.

**미러가 게시하는 모든 메시지가 대상입니다. 최종 답변뿐 아니라 진행 상황 메시지도 포함됩니다** (#683). Discord는 Markdown 표를 전혀 그려 주지 않으므로, 렌더링되지 않은 표는 `|` 문자가 그대로 늘어선 덩어리로 보입니다. 그리고 중간 메시지는 턴이 끝난 뒤에도 스레드에 남습니다. 따라서 최종 답변에만 적용하면 어떤 표가 읽을 수 있는 상태일지 읽는 사람이 예측할 수 없었습니다.

**이미지는 표가 들어 있는 메시지에 붙습니다.** 2000자를 넘는 답변은 여러 메시지로 나뉘는데, 각 표의 PNG는 그 표가 들어 있는 메시지에 첨부됩니다(분할 때문에 두 행 사이에서 잘린 표는 표가 시작되는 메시지에 통째로 그려집니다). #750 이전에는 모든 이미지가 *마지막* 메시지에 붙었기 때문에, 앞부분의 표는 그 자리에 파이프 문자 그대로 남고 그림은 한참 아래의 관계없는 문단 밑에 달려 있었습니다. `progress.txt`는 지금도 마지막 메시지에 붙습니다.

**메시지 하나에서 이미지가 되는 표는 최대 10개입니다.** 이것은 Discord의 첨부 파일 상한이며, 이를 넘기면 텍스트를 포함한 전송 전체가 실패합니다. 그래서 열 번째를 넘는 표는 메시지를 통째로 잃는 대신 원본 markdown 그대로 남습니다. 이 한도는 턴 단위가 아니라 메시지 단위입니다. 진행 상황 메시지마다 각자의 한도가 있습니다. 답변에 `progress.txt`도 함께 실리면 그 파일이 한 자리를 차지하고 표에는 나머지 아홉 자리가 돌아갑니다.

**셀 안의 인라인 markdown은** 그리기 전에 **일반 텍스트로 정리됩니다.** 이미지는 상호작용이 가능하게 만들 수 없으므로, 원본 문법을 남겨 두면 잡음만 새어 나오기 때문입니다. `[label](url)` / `![alt](url)`은 `label`이 되고(URL은 이미지 안에서 클릭할 수 없어 어수선하게만 만듭니다), `**bold**` / `*italic*` / `` `code` ``는 안쪽 텍스트만 남습니다. 밑줄을 이용한 강조(`_x_`, `__x__`)는 의도적으로 건드리지 않습니다. `_is_allowed` / `__init__` 같은 식별자가 망가지지 않게 하기 위해서입니다.

**`~~strike~~`는 예외로, 실제 취소선으로 그려집니다.** 그림은 글자 위에 줄을 그어 *보여 줄 수* 있고, Claude는 표에서 취소선을 "이 행은 제외되는 행"이라는 뜻으로 사용합니다. Discord는 메시지 본문에서 취소선을 표시하므로, 이미지에서 이를 일반 텍스트로 바꿔 버리면 둘이 서로 어긋나고 표의 의미가 뒤집힙니다. 취소선은 줄바꿈된 여러 줄에 걸쳐 글자를 따라가며, 셀 전체뿐 아니라 셀의 일부(`~~a~~ b`)에도 적용됩니다.

**설치:**

```bash
uv add "c-lord[table]"
# 또는: pip install "c-lord[table]"
```

> 같은 `[table]` extra는 **`/tmux-screenshot`** 명령어에도 쓰입니다. 현재 tmux 페인의 PNG를 게시하는 디버그 명령어입니다(ANSI 색상, 레이아웃, 상태 램프가 그대로 유지됩니다). 이 extra가 없으면 명령어는 이미지 대신 설치 안내로 답합니다.

**글꼴 지원 (CJK 텍스트 + 컬러 이모지):**

c-lord는 텍스트와 이모지를 따로 렌더링하며, 각 그룹에서 처음 발견되는 글꼴을 사용합니다.

| 글꼴 파일 경로 | 그룹 / 비고 |
|---|---|
| `~/.local/share/fonts/NotoSansJP.ttf` | **텍스트** — 일본어에 권장 |
| `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` | 텍스트 — JP / ZH / KO 지원 |
| `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | 텍스트 — 최후의 수단 (라틴 문자만) |
| `~/.local/share/fonts/NotoColorEmoji.ttf` | **컬러 이모지** — 🟢🔴🟡✅⚠️가 풀 컬러로 렌더링됩니다 |
| `/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf` | 컬러 이모지 — 시스템 경로의 대안 |
| `~/.local/share/fonts/NotoEmoji-Regular.ttf` | 흑백 이모지 — 컬러 글꼴이 없을 때의 대체 (색은 없지만 깨진 글자는 나오지 않습니다) |

컬러 이모지 글꼴이 없으면 c-lord는 흑백 이모지 글꼴(글리프 모양만, 색 없음)을 대신 쓰고, 그것도 없으면 문자를 그대로 그립니다. **CBDT** 형식의 Noto Color Emoji를 사용하세요. Pillow가 `embedded_color=True`로 렌더링합니다.

> **스크린샷에 일본어 텍스트 대신 빈 네모(□)가 보이나요?** 그것은 버그가 아니라 *이* 컴퓨터/사용자에 글꼴이 없어서 생기는 일입니다. 위 경로 중 어느 것도 찾지 못해 라틴 문자 전용 글꼴로 렌더링된 것입니다. 글꼴은 사용자별·호스트별로 설치되므로(`~/.local/share/fonts/`), 다른 곳에서 이미 잘 동작하더라도 다른 Linux 사용자 계정이나 새 호스트에는 따라오지 않습니다.
>
> **터미널 명령을 직접 실행하는 것이 익숙하지 않아도 괜찮습니다. 아래 블록을 AI 어시스턴트**(사용 중인 Claude Code 세션이나 c-lord의 Claude 스레드 자체)**에게 그대로 넘기고** 대신 실행해 달라고 하세요. 이 블록은 AI가 혼자 실행해도 안전하도록 작성되어 있습니다. 홈 디렉터리에만 쓰고, `sudo`가 필요 없으며, `~/.local/share/fonts/` 바깥은 건드리지 않습니다.

**일본어 + 컬러 이모지 빠른 설치 (Linux):**
```bash
mkdir -p ~/.local/share/fonts
# 일본어 텍스트
curl -Lo ~/.local/share/fonts/NotoSansJP.ttf \
  https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJK-Regular.ttc
# 컬러 이모지 (CBDT)
curl -Lo ~/.local/share/fonts/NotoColorEmoji.ttf \
  https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf
fc-cache -fv
```

긴 셀은 정해진 너비 안에서 줄바꿈되므로(`MAX_COL_WIDTH`, 표시 너비를 기준으로 하여 CJK 문자는 두 칸으로 계산) 넓은 표도 모바일에서 읽기 쉽습니다.

> **다른 CJK 언어 등(중국어, 한국어, 아랍어, 태국어 등)에 관한 참고:** 대상 문자를 지원하는 글꼴(예: 중국어 간체는 Noto Sans SC, 한국어는 Noto Sans KR)을 설치하고, 그 경로를 `c_lord/discord_ui/table_renderer.py`의 `_JP_FONT_PATHS` 목록에 추가하세요. 다국어 글꼴 감지를 추가하는 기여를 환영합니다.

## 예약 작업

예약 작업은 c-lord가 일정한 간격으로 대신 실행해 주는 프롬프트입니다. c-lord가 관리하는 것은 *언제*뿐입니다. *무엇을* 할지, 즉 무엇을 확인하고, 무엇을 게시하고, 어떻게 같은 일을 반복하지 않을지는 모두 프롬프트에 들어 있습니다. 작업은 SQLite에 저장되고 봇이 실행되는 동안 [REST API](#rest-api)로 추가, 변경, 삭제하므로 코드 변경도 재시작도 필요 없습니다. 보통은 스레드에서 Claude에게 등록해 달라고 요청하는 방식으로 만듭니다. Claude는 `CLORD_API_URL`로 API 주소를 알고 있습니다.

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

`name`, `prompt`, `interval_seconds`, `channel_id`는 모두 필수이고, 채널은 `/clord-init`으로 저장소에 연결되어 있어야 합니다. `working_dir`과 `run_immediately`는 선택 사항입니다. `PATCH /api/tasks/{id}`에 `{"enabled": false}`를 보내면 작업을 삭제하지 않고 일시 중지합니다. 루프 하나가 30초마다 깨어나 실행할 때가 된 작업을 시작합니다. 이전 실행이 아직 끝나지 않은 작업은 중복 실행하지 않고 건너뜁니다.

실행될 때마다 `🔄 **[Scheduled]** <task name>` 메시지를 게시하고, 그 메시지에 스레드를 열고, 작업의 `working_dir`에서 tmux 창을 만들어 거기서 Claude를 실행합니다. 답변은 다른 턴과 마찬가지로 스레드에 도착합니다. 예약 실행이 어떤 모습이어야 하는지(그리고 무엇이 버그인지)는 [docs/specs/scheduled-tasks.md](../specs/scheduled-tasks.md)에 정리되어 있습니다.

## GitHub 및 CI 자동화

Discord 웹후크에 게시할 수 있는 것이라면 무엇이든 Claude에게 작업을 맡길 수 있습니다. 누가 프롬프트를 쓰느냐에 따라 두 가지 방법이 있습니다.

**보내는 쪽이 프롬프트를 쓰는 경우.** 저장소에 연결된 채널에서 `!clord`로 시작하는 웹후크 메시지는 `/clord`와 똑같이 세션을 엽니다. 웹후크 URL을 가지고 있다는 것이 곧 권한이므로, URL을 비밀 정보로 취급하세요.

**프롬프트가 내 쪽에 고정되어 있는 경우.** `WebhookTriggerCog`는 메시지 접두사를 코드에서 정의해 둔 프롬프트에 대응시킵니다. 웹후크는 *어느* 트리거를 실행할지만 고를 수 있고, Claude가 따를 텍스트를 직접 넣을 수는 없습니다. CI에는 이쪽이 더 안전한 선택입니다. 이 cog는 기본으로 등록되지 않으므로 [직접 만든 봇](#c-lord를-라이브러리로-사용하기)에 추가하세요.

```python
from c_lord import ClaudeConfig, WebhookTrigger, WebhookTriggerCog

docs_sync = WebhookTrigger(
    prompt="Read the latest commits on main and update the docs to match. Open a PR.",
    working_dir="/srv/checkouts/my-project",
    timeout=900,  # 초
)

await bot.add_cog(
    WebhookTriggerCog(
        bot=bot,
        runner=ClaudeConfig(model="sonnet"),
        triggers={"🔄 docs-sync": docs_sync},      # 메시지 접두사 → 실행할 내용
        channel_ids={123456789012345678},          # 이 채널만 감시
        allowed_webhook_ids={234567890123456789},  # 이 웹후크만 허용
    )
)
```

`WebhookTrigger`는 `dangerously_skip_permissions=False`를 넘기지 않는 한 Claude Code의 권한 확인을 건너뜁니다. 프롬프트에 답해 줄 사람이 없기 때문입니다. `channel_ids`와 `allowed_webhook_ids`를 제한해야 하는 이유가 하나 더 있는 셈입니다.

**트리거가 실행되면 일어나는 일:** 웹후크 메시지에 스레드가 만들어지고, Claude는 트리거의 `working_dir`(없으면 runner의 것)에 있는 자체 tmux 창에서 실행되며, 답변은 그 스레드에 게시됩니다. 창을 만들 수 없으면(예: 호스트에서 tmux를 쓸 수 없는 경우) 실행은 시작되지 않습니다. 스레드에 이유가 표시되고, 웹후크 메시지에 ❌가 붙고, 봇 소유자가 멘션됩니다(#629).

### 예시: 문서 자동 동기화 (직접 만든 봇에 구현)

> **참고:** 이것은 직접 만든 봇에 구현할 수 있는 패턴입니다. c-lord에는 문서 동기화 워크플로가 포함되어 있지 않습니다. 참고용 구현은 [EbiBot](https://github.com/ebibibi/discord-bot)을 보세요.

`main`에 푸시될 때마다 봇이 Claude Code를 트리거해 다음을 수행하게 할 수 있습니다.
1. 최신 변경 사항을 pull하고 diff를 분석
2. 영어 문서를 업데이트
3. 일본어(또는 원하는 대상 언어)로 번역
4. 두 언어로 된 요약과 함께 PR 생성
5. auto-merge 활성화 — CI가 통과하면 자동으로 머지됩니다

**GitHub Actions (저장소에 추가):**

```yaml
# .github/workflows/docs-sync.yml
on:
  push:
    branches: [main]
jobs:
  ask-claude:
    # Claude가 직접 만든 문서 커밋에는 [docs-sync]가 붙습니다. 이 줄이 없으면 그 커밋이 워크플로를 다시 실행시킵니다
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

봇 쪽은 위에서 본 `WebhookTriggerCog`이며, `🔄 docs-sync`가 트리거입니다. Claude가 만든 PR이 `main`에 머지되면 그 푸시가 워크플로를 다시 실행시킬 수 있습니다. `if:` 줄이 이 반복을 막아 주는데, 프롬프트에서 Claude에게 커밋 메시지에 `[docs-sync]`를 넣으라고 지시해 두어야 합니다.

### 턴이 끝났을 때 알림 받기

작업을 맡긴 스크립트가 스레드를 폴링할 필요는 없습니다. `CLORD_EVENT_LOG`나 `CLORD_EVENT_WEBHOOK_URL`을 설정하면 c-lord는 턴이 끝날 때마다, 마지막 답변이 게시된 뒤에 이벤트 하나(`completion`, `blocked` 또는 `failure`)를 내보냅니다([사양](../specs/turn-end-events.md)).

### 웹후크로 봇 업그레이드하기

`AutoUpgradeCog`는 c-lord를 패키지로 설치해 쓰는 봇을 위한 것입니다. `WebhookTriggerCog`와 마찬가지로 직접 추가합니다.

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

`trigger_prefix`(기본값 `🔄 upgrade`)로 시작하는 웹후크 메시지가 도착하면 cog는 스레드를 열고 각 단계를 거기에 보고합니다.

1. `working_dir`에서 `uv lock --upgrade-package <package_name>`을 실행한 다음 `uv sync`를 실행합니다(각각 `upgrade_command` / `sync_command`로 바꿀 수 있습니다).
2. 어느 스레드에서 턴이 실행 중인지 기록한 다음, 실행 중인 작업이 끝나기를 기다립니다. `active_count` 프로퍼티를 제공하는 모든 cog(`DrainAware` 프로토콜)에 물어보며, 직접 만든 cog도 포함됩니다. `drain_timeout`(300초)이 지나면 더 기다리지 않습니다.
3. `restart_command`를 실행합니다. 지정하지 않았으면 아무것도 재시작하지 않고, 스레드에 업그레이드가 완료되었다고 표시합니다.

재시작 후에는 2단계에서 기록해 둔 스레드에 c-lord가 재시작되었다는 한 줄짜리 알림이 조용히 게시됩니다. Claude에게 다시 프롬프트를 보내지는 **않습니다**. `claude` 프로세스는 tmux 안에 있고 멈춘 적이 없으므로 재개할 것이 없기 때문입니다(#406).

`upgrade_approval=True`는 1단계 전에 승인을 묻고, `restart_approval=True`는 새 버전이 설치된 뒤 2단계와 3단계 전에 승인을 묻습니다. 어느 쪽이든 업그레이드 스레드에서 ✅ 반응을 달거나 Approve 버튼을 눌러 답합니다. `slash_command_enabled=True`는 `/upgrade`를 추가하며, 이 명령어는 웹후크 없이 같은 파이프라인을 시작합니다. 봇을 조작하도록 허용된 사용자만 실행할 수 있습니다. 채널에서도 스레드에서도 동작하며, 스레드에서 실행하면 업그레이드 스레드는 상위 채널에 열립니다. 업그레이드가 이미 진행 중이면 그 사실을 알려 줍니다.

자체 클론에서 실행하는 c-lord에는 이런 것이 전혀 필요 없습니다: `git pull && systemctl --user restart c-lord.service`.

## REST API

알림, 예약 작업, 세션 생성을 위한 c-lord의 제어 평면입니다. 실행할 때마다 항상 시작되며, 답변이 전달되는 방식과는 관계가 없습니다(#712/#543). aiohttp가 필요합니다.

```bash
uv add "c-lord[api]"
```

API는 `CLORD_API_HOST:CLORD_API_PORT`(기본값 `127.0.0.1:8080`)에 바인딩됩니다. `CLORD_API_PORT`를 **설정하지 않았다면** 포트가 이미 사용 중이어도 문제가 되지 않습니다. API가 다음 빈 포트로 옮겨 가고(8081, 8082, … 그다음에는 아무 빈 포트) 어디로 갔는지 로그에 남기므로, 한 호스트의 두 클론이 아무 설정 없이 각자 API를 갖게 됩니다(#258). 직접 **설정한** 포트는 그대로 사용됩니다. 그 포트가 사용 중이면 봇은 시작에 실패하는 대신 WARNING을 남기고 API 없이 실행됩니다. ASCII 숫자가 아닌 값(`²`, 단어)은 `CLORD_API_PORT`를 명시한 WARNING과 함께 설정되지 않은 것처럼 무시되며, 숫자 앞뒤의 공백은 무시됩니다(#1218). 65535를 넘는 숫자는 직접 설정했지만 사용할 수 없는 포트로 취급됩니다. `CLORD_API_PORT`를 명시한 WARNING이 나오고 봇은 API 없이 실행됩니다(#1237). 마찬가지로 `CLORD_TRUSTED_BOT_IDS`의 항목 중 ASCII 숫자가 아니거나 Discord id보다 긴(20자리) 것은 시작 시 WARNING과 함께 무시되고, 목록의 나머지 id는 그대로 동작합니다. 여기에는 전각 숫자(`１２３`)도 포함됩니다. 예전에는 이것이 id 123으로 읽혔습니다. id는 ASCII 숫자로 적으세요.

모든 세션의 Claude는 API의 위치를 `CLORD_API_URL` 환경 변수로 전달받습니다. 이 값은 실제로 바인딩된 포트이므로 `CLORD_API_PORT`와 손으로 맞춰 줄 필요가 없습니다. `CLORD_API_URL`은 세션이 다른 곳(예: 리버스 프록시)을 가리키게 하고 싶을 때만 직접 설정하세요. API가 실행되고 있지 않으면 세션에는 `CLORD_API_URL`이 아예 전달되지 않습니다.

**봇을 실행하는 Unix 사용자에게만 응답합니다** (#457). `POST /api/spawn`은 Claude Code 세션을 시작하므로, 이 포트에 접근할 수 있다는 것은 그 사용자의 셸을 얻는 것과 같습니다. 그리고 루프백은 *네트워크* 경계이지 *UID* 경계가 아닙니다. 다중 사용자 호스트의 모든 계정이 `127.0.0.1`에 연결할 수 있습니다. 그래서 접속한 상대의 UID를 `/proc/net/tcp`에서 읽어 봇 자신의 UID와 대조하며, `/api/health`도 예외가 아닙니다. 그 밖의 모든 요청은 `403`을 받습니다. 설정할 것은 없으며, 본인의 호출은 지금처럼 그대로 동작합니다. 다른 사용자나 다른 호스트를 허용하려면 `CLORD_API_SECRET`을 설정하고 상대가 `Authorization: Bearer …`를 보내게 하세요. [docs/SECURITY.md](../SECURITY.md#rest-api-exposure-712-457)를 참고하세요.

| 메서드 | 경로 | 하는 일 |
|---|---|---|
| GET | `/api/health` | 동작 여부 확인 |
| POST | `/api/spawn` | 새 스레드를 열고 `prompt`로 그 안에서 Claude를 시작합니다 (`channel_id`, `thread_name`은 선택). 스레드가 만들어지는 즉시 응답합니다 |
| POST | `/api/threads/{thread_id}/messages` | 스레드에 `content`를 게시합니다 |
| POST | `/api/threads/{thread_id}/sleep` | 유휴 절전과 같은 방식으로 그 스레드의 워크스페이스를 절전 상태로 만듭니다 |
| POST | `/api/notify` | 알림을 게시합니다 (`message`, 선택 항목 `title`, `color`, `channel_id`) |
| POST | `/api/schedule` | 알림을 나중에 게시합니다 (`message`, ISO 8601 형식의 `scheduled_at`) |
| GET | `/api/scheduled` | 아직 대기 중인 알림을 나열합니다 |
| DELETE | `/api/scheduled/{id}` | 하나를 취소합니다 |
| POST | `/api/tasks` | [예약 작업](#예약-작업)을 등록합니다 |
| GET | `/api/tasks` | 작업을 나열합니다 |
| PATCH | `/api/tasks/{id}` | 작업을 변경합니다: `enabled`, `prompt`, `interval_seconds`, `working_dir` |
| DELETE | `/api/tasks/{id}` | 작업을 삭제합니다 |
| POST | `/api/mark-resume` | 봇을 직접 재시작하기 전에 스레드를 기록해 두어, 다음 시작 때 그 스레드가 재시작 알림을 받게 합니다 |
| GET | `/api/lounge` | 최근 AI Lounge 메시지를 읽습니다 (세션은 lounge에 대해 전달받지 못하므로, 직접 구성하지 않는 한 여기에 게시되는 것은 없습니다 — #758) |
| POST | `/api/lounge` | AI Lounge에 메시지를 게시합니다 (선택 항목 `label`) — c-lord의 세션이 스스로 호출하지는 않습니다 (#758) |

모든 필드, 상태 코드, 예외 상황은 [docs/COMMANDS.md](../COMMANDS.md#rest-api)에 있습니다. 예시 두 가지를 봇과 같은 Unix 사용자로 실행하는 경우이므로 헤더가 필요 없습니다.

```bash
# 스크립트에서 세션 시작 — 응답에 스레드의 ID가 담겨 돌아옵니다
curl -X POST http://127.0.0.1:8080/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review the newest open PR and summarise the risks", "thread_name": "PR review"}'

# 홈 채널에 무슨 일이 있었는지 알리기
curl -X POST http://127.0.0.1:8080/api/notify \
  -H "Content-Type: application/json" \
  -d '{"title": "CI", "message": "Nightly build is green"}'
```

## c-lord를 라이브러리로 사용하기

c-lord는 평범한 Python 패키지이고, `c-lord start`는 그 위에 만들어진 프로그램 하나일 뿐입니다. 직접 만든 봇 안에 넣고 자신의 cog와 함께 쓰려면, 설치한 뒤 `setup_bridge()`를 호출하세요.

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
        # await bot.add_cog(YourCog(bot))   ← 직접 만든 cog는 여기에 추가합니다
        await bot.start(os.environ["DISCORD_BOT_TOKEN"])


asyncio.run(main())
```

`setup_bridge()`는 c-lord의 표준 cog를 등록합니다. 위와 같이 호출하면 아홉 개입니다: 채팅, 채널과 저장소의 연결, 스킬, 워크스페이스 명령어, 30일 정리, 스케줄러, 트랜스크립트 미러, 로그인 중계, `/version`. 의도된 설계입니다. 나중에 c-lord에 추가되는 표준 기능은 패키지를 업데이트하기만 하면 코드 변경 없이 내 봇에 들어옵니다. `WebhookTriggerCog`와 `AutoUpgradeCog`는 여기에 포함되지 않으므로, 이 둘은 위에서 본 것처럼 `bot.add_cog()`로 직접 추가하세요.

```bash
uv lock --upgrade-package c-lord && uv sync
```

봇 클래스로는 `ClaudeDiscordBot`을 사용하세요. c-lord의 cog들은 홈 채널처럼 이 클래스가 가지고 있는 값을 읽습니다. 나머지는 독립 실행 진입점인 [`c_lord/main.py`](../../c_lord/main.py)를 참고하면 됩니다. `.env` 읽기, 단일 인스턴스 잠금, REST API 시작, 시그널을 받았을 때의 종료 처리가 들어 있습니다.

## 아키텍처

### Discord와 tmux의 대응 관계

"Discord 스레드 = Claude Code 세션"이라는 아이디어는 tmux 레이아웃 위에 놓여 있습니다. 스레드가 실제로 어디에 있는지 알면 디버깅과 연동이 훨씬 쉬워집니다(`tmux attach`로 세션을 실시간으로 지켜볼 수 있습니다).

| 단위 | tmux | 대응 |
|------|--------------|---------|
| 스레드가 연결된 저장소 | tmux 세션 | 1:1 |
| 스레드 | tmux 창 | 1:1 |

- **세션은 오직 채널에서만 만들어집니다.** `/clord`는 *새* 스레드를 엽니다. 스레드 안에서 실행하면 그 스레드의 세션을 이어 가거나, 기록이 정리된 세션이라면 다시 연결할지 묻거나, 처음부터 c-lord의 스레드가 아니었다면 거부합니다(#551). 일반 대화 스레드를 Claude 세션으로 바꿀 수는 없습니다. 기존 스레드를 넘겨받는 명령어는 의도적으로 두지 않았습니다. [docs/COMMANDS.md](../COMMANDS.md#chat--sessions) 참고.
- **저장소 1개 = tmux 세션 1개입니다. 채널 1개가 아닙니다.** 세션 이름은 스레드가 연결된 저장소에서 나옵니다. 스레드 자체에 연결된 저장소가 있으면 그것을(`/clord-thread-init` 또는 `/clord repo:<url>`), 없으면 채널의 저장소(`/clord-init`)를 따릅니다. 연결이 없는 채널은 기본 `clord` 세션을 사용합니다. 따라서 스레드들이 서로 다른 저장소에 연결된 채널은 여러 세션에 걸쳐 있게 되고, 같은 저장소에 연결된 두 채널은 세션 하나를 공유합니다. 특정 스레드가 어디에 있는지, 그리고 왜 이렇게 정했는지는 [docs/specs/tmux-layout.md](../specs/tmux-layout.md)에 자세히 적혀 있습니다.
- **스레드 1개 = tmux 창 1개 = Claude Code 세션 1개.** 각 스레드는 Claude Code 세션 하나가 실행되는 자체 창(`w1`, `w2`, …)을 갖습니다. 그러니 스레드에서 주고받는 대화가 *곧* 그 Claude 세션과 주고받는 대화이며, 답글은 `--resume`으로 세션을 이어 갑니다. 창 번호는 채널 안이 아니라 **세션 안에서** 고유합니다.

이 설계의 "이유"는 [docs/PHILOSOPHY.md](../PHILOSOPHY.md)를 참고하세요.

### 답변이 Discord에 도착하는 경로

```text
Discord 스레드에 있는 사용자                  그 스레드의 tmux 창에서 도는 Claude Code
        │                                                    │
        │  메시지가 페인에 입력됩니다  ─────────────────────▶│
        │                                                    │  작업: 읽기, 편집, 도구 실행
        │                                                    ▼
        │                          ~/.claude/projects/<dir>/<session>.jsonl
        │                          (Claude Code가 스스로 기록하는 트랜스크립트)
        │                                                    │
        ◀─── c-lord가 이 파일을 따라 읽어 내용을 게시합니다 ─┘
```

c-lord는 Claude의 답변을 터미널 화면에서 읽어 오지 않고, Claude에게 답변을 게시해 달라고 요청하지도 않습니다. Claude Code가 이미 기록하고 있는 트랜스크립트를 읽어 그대로 전달합니다. 화면 스크래핑은 터미널 UI의 조각이 Discord로 계속 새어 나오는 문제가 있었고, Claude에게 게시를 맡기는 방식은 Claude가 잊어버릴 때마다 턴 하나가 통째로 사라졌습니다. 트랜스크립트에는 두 문제 모두 없으며, 이것이 답변이 지나가는 유일한 경로입니다.

페인은 여전히 감시하지만 텍스트를 얻기 위해서가 아닙니다. 턴이 끝났음을 알아채고, Claude Code가 보여 주는 메뉴(`AskUserQuestion`, 계획 승인)를 Discord 버튼으로 바꾸고, Claude Code의 권한 프롬프트를 수락하고, c-lord가 인식하지 못하는 프롬프트를 보고하기 위해서입니다. 이 모든 과정은 [docs/ARCHITECTURE.md](../ARCHITECTURE.md)가 차례로 설명하고, 그 이유는 [docs/DESIGN_DECISIONS.md](../DESIGN_DECISIONS.md)에 기록되어 있습니다.

### 파일 위치

```text
c_lord/
  cli.py            `c-lord` 명령어: setup, start, version
  main.py           독립 실행 진입점 — .env를 읽고, 잠금을 잡고, 전부 시작합니다
  setup.py          setup_bridge(): 한 번의 호출로 표준 cog를 등록합니다
  bot.py            ClaudeDiscordBot
  tmux.py           tmux 세션과 창. claude 시작, 페인에 입력하기
  session_dir.py    스레드마다 git 클론 하나
  claude/           tmux 페인 위에서 도는 턴 단위 러너, CLI 설정, 컨텍스트 사용량
  transcript/       Claude Code의 트랜스크립트를 따라 읽어 Discord용으로 다듬기. 서브에이전트 로그
  cogs/             채팅, 채널↔저장소 연결, 스킬, 워크스페이스 명령어, 스케줄러,
                    웹후크 트리거, 자동 업그레이드, 트랜스크립트 미러
  discord_ui/       상태 램프, 질문 버튼, 턴 진행 상황 줄, 표와 페인 이미지
  database/         SQLite: 세션, 연결, 예약 작업, 알림, 대기 중인 메뉴
  ext/api_server.py REST API
  skills/           각 세션에 넣어 주는 discord-read 스킬
deploy/             systemd 유닛
scripts/            install-systemd.sh, staging.sh, 증거 자료 및 릴리스 도구
docs/               가이드, 그리고 docs/specs/ — 각 기능이 어떻게 동작해야 하는지
```

### c-lord인 것과 아닌 것

- **AI가 아니라 다리입니다.** c-lord에는 자체 프롬프트도, 도구도, 메모리도 없습니다. 그것은 모두 Claude Code의 것이며, Claude Code에 설정해 둔 것(`CLAUDE.md`, 스킬, MCP 서버, 훅)은 스레드에서도 그대로 동작합니다. 같은 프로그램이기 때문입니다.
- **API가 아니라 실제 CLI입니다.** 각 스레드는 터미널에서 실행하는 것과 같은 대화형 `claude`를 실행합니다. c-lord는 Anthropic API를 호출하지 않습니다.
- **tmux는 그대로 남습니다.** c-lord는 tmux를 대체하지 않고, Discord에서 tmux를 관리할 수 있게 해 줍니다. 페인은 언제든 attach할 수 있게 그 자리에 있습니다.
- **연결이 끊겨도 살아남는 세션.** Claude는 서버에서 실행됩니다. 노트북이 절전 상태가 되거나, 기차가 터널에 들어가거나, 봇이 재시작되어도 멈추지 않습니다.
- **판단은 Discord에서 하므로, Discord에 판단할 만큼의 정보가 보여야 합니다.** 진행 상황, 결과, 파일, 실패가 모두 스레드에 남습니다. 실패했을 때는 조용해지는 대신 무엇이 실패했고 다음에 무엇을 해야 하는지 알려 줍니다.
- **일반적인 사용에는 설정할 것이 없습니다.** 새 기능은 합리적인 설정과 함께 기본으로 켜져 있고, 사용하는 쪽은 패키지를 업데이트하기만 하면 됩니다.

이런 판단의 배경은 [docs/PHILOSOPHY.md](../PHILOSOPHY.md)(일본어)에 있습니다.

## 보안

c-lord에 말을 건다는 것은 c-lord가 있는 컴퓨터에서 명령을 실행한다는 뜻입니다. 기본값은 이 점을 전제로 정해져 있습니다.

- **사용자 인가** — `allowed_user_ids` / `CLORD_ALLOWED_ROLE`은 누가 Claude를 호출할 수 있는지 제한합니다. **둘 다 설정하지 않으면 Discord 애플리케이션의 소유자만 봇을 사용할 수 있습니다.** c-lord에 말을 걸면 호스트에서 셸 명령이 실행되므로, "설정하지 않음"이 "누구나"를 뜻하지 않습니다(#713). `CLORD_ALLOW_ANYONE=1`은 의도적으로 서버 전체에 개방합니다(시작할 때 그 사실을 알립니다). [docs/specs/authorization-default.md](../specs/authorization-default.md) 참고
- **REST API는** `CLORD_API_SECRET`을 설정하지 않는 한 **봇 자신의 Unix 사용자에게만 응답합니다**([REST API](#rest-api) 참고).
- **중간에 셸이 없습니다.** c-lord가 시작하는 모든 프로세스는 셸을 거치지 않고 인수 목록으로 시작되며(`asyncio.create_subprocess_exec`), 프롬프트는 `--` 뒤에 전달되므로 플래그로 해석될 수 없습니다.
- **사용하기 전에 검사합니다.** 세션 ID는 세션 ID의 형식을 갖추어야만 `--resume`에 전달됩니다. 스킬 이름과 저장소 URL도 같은 방식으로 검증됩니다.
- **봇 토큰은 상속을 통해 세션으로 새어 나가지 않습니다.** 토큰은 tmux 환경과 `claude` 명령줄의 환경에서 제거됩니다. 이것은 사고를 막기 위한 것이지, 작정하고 찾으려는 세션을 막는 것은 아닙니다. `.env`는 같은 Unix 사용자가 읽을 수 있는 파일입니다.

위협 모델과 의도적으로 감수하는 위험은 [docs/SECURITY.md](../SECURITY.md)에 있습니다.

## 개발

```bash
git clone https://github.com/yousan/c-lord.git
cd c-lord
uv sync --dev
make setup                                # 클론마다 한 번: pre-commit 훅을 켭니다

uv run pytest tests/ -v --cov=c_lord
uv run ruff check c_lord/ && uv run ruff format --check c_lord/
uv run pyright c_lord/
```

CI는 이 검사들을 Python 3.10, 3.11, 3.12에서 실행합니다. 이 검사들은 mock을 사용하므로, CI가 통과했다고 해서 변경 사항이 실제 Discord와 실제 tmux에서 동작한다는 것이 확인되지는 않습니다. 그래서 변경 사항은 머지 전에 스테이징 봇에서 동작하는 모습도 보여야 합니다. pull request에 필요한 것은 [CONTRIBUTING.md](../../CONTRIBUTING.md)에, 스테이징 봇에 관한 설명은 [docs/STAGING.md](../STAGING.md)에 있습니다.

## 이 프로젝트가 만들어지는 방식

**이 전체 코드베이스는 [Claude Code](https://docs.anthropic.com/en/docs/claude-code)**에 의해 작성되었습니다. 인간 저자 ([@ebibibi](https://github.com/ebibibi))는 자연어로 요구사항과 방향을 제공했지만 소스 코드를 직접 읽거나 편집하지 않았습니다.

위 문단은 업스트림 프로젝트인 claude-code-discord-bridge에 관한 설명입니다. 이 프로젝트가 MIT 라이선스에 따라 c-lord에 초기 아키텍처를 제공했습니다. 현재 이 파생 프로젝트는 [@yousan](https://github.com/yousan)이 유지 관리하고 있으며, 요구사항을 정의하고 pull request를 리뷰하고 모든 변경을 승인합니다. 구현은 Claude Code가 합니다.

평소의 흐름은 이렇습니다. 변경은 인수 기준이 적힌 Issue에서 시작합니다. Claude Code는(대개 c-lord 스레드에서 실행되므로, c-lord는 c-lord로 만들어지는 셈입니다) 실패하는 테스트를 작성하고, 그 테스트를 통과시키고, 변경 사항이 스테이징 봇에서 동작하는 모습을 스크린샷으로 보여 주고, pull request를 엽니다. 두 개의 필수 체크가 `main`을 지키고 있으며, pull request는 [Definition of Done](../../CLAUDE.md#definition-of-done-dod--single-source-of-truth)의 모든 항목이 충족되었을 때만 머지됩니다.

c-lord 자체의 히스토리는 2026-02-22에 그 업스트림 프로젝트의 사본으로 시작합니다. 버그 리포트와 pull request는 사람이 보낸 것이든 에이전트가 보낸 것이든 환영합니다.

## 실제 예시

**[EbiBot](https://github.com/ebibibi/discord-bot)** — 이 프레임워크를 기반으로 구축된 개인 Discord 봇. 자체 봇을 구축하기 위한 참고 자료로 활용하세요.

EbiBot은 ebibibi 본인의 봇이며, c-lord가 아니라 업스트림인 claude-code-discord-bridge 위에 만들어졌습니다.

## 영감을 준 프로젝트

- [@ebibibi](https://github.com/ebibibi)의 [claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge) — 이 프로젝트의 기반이 된 업스트림 프로젝트입니다.
- [OpenClaw](https://github.com/openclaw/openclaw) — 이모지 상태 반응, 메시지 디바운싱, 코드 펜스를 고려한 메시지 분할
- [claude-code-discord-bot](https://github.com/timoconnellaus/claude-code-discord-bot) — CLI 실행 + stream-json 방식
- [claude-code-discord](https://github.com/zebbern/claude-code-discord) — 권한 제어 패턴
- [claude-sandbox-bot](https://github.com/RhysSullivan/claude-sandbox-bot) — 대화마다 스레드 하나를 쓰는 모델

## 라이선스

[MIT](../../LICENSE)
