# benchbook: 테스트가 확인하기 전까지 "완료"는 주장일 뿐이다

**코딩 에이전트가 "끝났다"고 보고하면 benchbook은 그 commit에서 검사를 다시 돌리고, 통과하려고 테스트를 약하게 만든
흔적을 잡아내며, 검증된 사실을 subagent, 세션, compaction 너머로 이어 준다.**

일을 조각으로 나눠 맡기는 리드 에이전트가 돌려받는 것은 "DONE a1b2c3d, 테스트 전부 통과" 같은 텍스트다. 그중 일부는
틀렸고, 일부는 assert를 지우거나 skip을 붙여서 초록불을 만들었다. 맞았던 보고도 다음 compaction이나 다음 세션에서
잊히고, 새 에이전트는 이미 실패한 접근을 다시 시도한다. benchbook은 저장소 옆에 Postgres 원장을 둔다. 보고는 완료
주장이 되고, 그 commit을 깨끗한 worktree에서 선언된 검사로 독립 재실행한 결과만이 그것을 `verified`로 바꾼다. 모든
세션은 누군가의 기억이 아니라 원장이 말하는 것에서 시작한다.

## 60초 시작 (Claude Code)

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook   # v0.1.0이 PyPI에 올라가면 `uv tool install benchbook`. Docker 불필요
benchbook install --no-cli       # benchbook hook plugin (--mcp를 붙이면 MCP 서버와 worker 에이전트도)
cd your-repo && benchbook init   # 저장소의 테스트와 lint 명령을 검사로 제안한다. .benchbook/은 git에 잡히지 않는다
```

그다음은 하던 대로 일하면 된다. 약속은 하나다. 구현자는 보고 끝에 `DONE <sha>`(또는 `commit: <sha>` 줄)를 쓰고, 조각
이름은 브랜치 이름, `piece: <이름>` 줄, `Benchbook-Piece:` commit trailer 중 하나로 정해진다.

## 세션에서 일어나는 일

| 언제 | benchbook이 하는 일 |
|---|---|
| 세션 시작, 재개, compaction 뒤 | 이 브랜치가 바꾼 범위의 brief(최대 1,500 token)를 준다. 검증된 조각과 확인 방법, "완료 보고, 재실행 안 됨", 버린 접근, 열린 발견, 점유된 경로가 들어 있다. `.benchbook/STATE.md`에도 쓴다 |
| compaction 직전 | 요약에 레코드 id와 상태를 그대로 남기라고 요청한다 |
| subagent가 `DONE <sha>`를 보고 | 그 commit을 깨끗한 worktree에서 선언된 검사로 백그라운드에서 다시 돌린다. 재실행은 `rerun@<host>`(orchestrator)가 하므로 통과하면 `verified`로 결정된다 |
| 다음 프롬프트, Bash 호출, 에이전트 결과 | 결과를 한 번 전한다: `benchbook: media-upload a1b2c3d: re-run passed (2 checks, 14.2s), tests not weakened, checked: re-run (c:7f3a91c2)` |
| 에이전트가 선언된 검사를 Bash로 실행 | 그 실행을 조각과 commit에 기록한다 |
| 에이전트가 조각 브랜치(`piece start`)에서 task를 완료로 표시 | 선언된 검사마다 HEAD에서 통과한 실행이 생길 때까지 task를 열어 두고, 돌릴 명령을 알려 준다 |
| 기록이 멈춤(dead 보고, hook 실패) | brief 첫 줄에 `benchbook: recording stopped: ...`와 고치는 방법을 쓴다 |

hook은 stderr에 쓰지 않고 항상 0으로 끝난다. benchbook 프로젝트 밖에서는 DB를 건드리지 않고 바로 끝난다.

## "검증됨"의 뜻

- **검사는 한 번 선언하고 고정한다.** `.benchbook/config.toml`의 `[checks]`에 명령을 적으면 원장이 그것을 고정한다.
  구현자는 자기 조각의 "통과" 기준을 바꿀 수 없고, 검사 정의를 바꾼 commit은 사람이 판정한다.
- **재실행은 독립적이다.** 보고된 commit의 깨끗한 worktree에서 리드나 hook의 processor가 돌린다. 구현자 자신의 실행도
  기록되지만 재실행으로 치지 않는다.
- **약해진 테스트는 초록불이어도 사람이 본다.** assert 삭제나 변경, 새 `skip`/`xfail` 표식, `conftest.py` 변경,
  merge-base 때보다 테스트를 적게 실행한 검사가 여기에 해당한다(검사마다 따로 비교한다). 테스트를 더한 것은 괜찮다.
- **환경 탓은 commit 탓이 아니다.** 구현자의 깨끗한 실행이 통과했고 merge-base에서도 같은 방식으로 실패할 때만 환경
  실패(빠진 `.env`, 내려간 서비스)로 본다. base에서 원래 실패하는 목표 테스트는 여전히 조각의 실패다.
- **실패는 기억된다.** 실패한 재실행은 실패 signature와 함께 버린 접근으로 남고, 다음 에이전트의 brief에 반복하지 말라고
  나온다.

자세한 내용: [docs/code.md](docs/code.md) (영어).

## 리드가 쓰는 명령

```bash
benchbook check verify media-upload a1b2c3d --reported-by implementer-1   # 손으로 재실행 (CI, 다른 하네스)
benchbook brief --here            # 이 브랜치가 바꾼 범위의 brief
benchbook status                  # 검증된 조각, 보고만 된 조각, 재실행 불일치, 열린 발견, hook 상태
benchbook get c:7f3a91c2          # 레코드 하나를 400 token 이내로
benchbook piece start media-upload --paths 'src/media/**'   # 경로를 점유해 두 구현자가 부딪히지 않게
```

`benchbook statusline`은 status line용 한 줄 집계를 출력한다. [GitHub Action](integrations/github-action/README.md)은
PR이 완료한 조각을 다시 돌리고, 독립 재실행이 확인한 완료를 댓글 하나에 정리한다.

## 다른 하네스

| 하네스 | 설치 | 세션 시작 brief | 도구 뒤 알림 | 검사 실행 기록 |
|---|---|---|---|---|
| Claude Code | `benchbook install` | SessionStart, compaction 뒤에도 | UserPromptSubmit, PostToolUse | Bash |
| Codex | `benchbook install --harness codex` | SessionStart, compaction 뒤에도 | PostToolUse | Bash, 출력에 종료 코드가 있을 때 |
| Cursor | `benchbook install --harness cursor` | sessionStart | postToolUse | Shell |
| Gemini CLI | `benchbook install --harness gemini` | SessionStart | AfterTool | `run_shell_command` |
| OpenCode V2 | [index.ts](integrations/opencode/README.md) 복사 | 첫 모델 요청 | 다음 모델 요청 | `shell` |
| Pi | [benchbook.ts](integrations/pi/README.md) 복사 | 첫 에이전트 턴 전 | 도구 결과 | `bash` |

보고 뒤 자동 재실행은 Claude Code에서만 한다. 다른 하네스에서는 `benchbook check verify`가 같은 일을 한다.
[docs/harnesses.md](docs/harnesses.md)는 읽는 필드마다 각 하네스 문서의 근거 문장을 인용한다.

## 지금까지 잰 것

**"끝났다"가 실제로 끝나지 않은 비율(SWE-bench Verified, 2026-11-23):** Claude Code(`claude-sonnet-5-5`)가 끝났다고
보고한 39건 중 7건이 benchbook의 재실행에서 선언된 체크를 통과하지 못했다. **17.9%, 95% 신뢰구간 9.0%~32.7%**다
(숨은 테스트만 보면 39건 중 5건, 12.8%). 끝났다고 보고하기 전에 테스트를 돌린 구현자는 39명 중 3명뿐이었다. 신뢰구간이
계획의 기준인 10%를 포함하지만, 점추정은 그 위다. visible 체크 자체 때문에 생긴 반박 2건을 포함한 자세한 내용은
[bench/results/2026-11-23-swe-verified/](bench/results/2026-11-23-swe-verified/README.md)에 있다. 하네스는
`bench/code/`다. `bench/boundary/`는 compaction과 재시작 뒤에 리드가 무엇을 아직 아는지 여섯 구성으로
잰다([PREREGISTRATION.md](bench/boundary/PREREGISTRATION.md)).

## 실험 swarm

benchbook은 실험을 돌리는 에이전트들의 연구 노트로 시작했고, 이 모드는 그대로다. worker는 실험이 끝나는 즉시(실패도)
기록하고, 돌리기 전에 이미 해본 것인지 확인하며, 리드에게는 3문장 요약과 레코드 id만 넘긴다. 리드는 대화록 대신 SQL로
만든 예산 안의 brief(최고 결과, 검증된 주장, 반복하지 말 것)를 읽는다.

```bash
cd your-repo && benchbook init --goal "검증 손실 최소화" --metric val_loss --direction min
benchbook install --no-cli --mcp
```

Claude Code에서 `/benchbook-mcp:benchbook-lead train.py의 val_loss 최소화, 3라운드, worker 4개`를 실행하고, `benchbook
serve` 후 http://127.0.0.1:7466 에서 진행을 본다. Claude Code 없이도 CLI와 Python SDK로 같은 일을 한다.

```bash
export BENCHBOOK_AGENT=worker-1   # 누가 기록하는지
benchbook exp tried  --config '{"lr": 3e-4, "warmup": 500}'
benchbook exp record --config '{"lr": 3e-4, "warmup": 500}' --metrics '{"val_loss": 2.91}' --seed 0
benchbook brief
```

### autoresearch를 GPU 여러 대에서

**GPU 여러 대를 하나의 연구실로: 같은 실험을 반복하지 않고, 결과를 잃지 않으며, 재현된 결과 위에서만 쌓는다.**

```bash
cd autoresearch && uv run prepare.py                         # karpathy/autoresearch 또는 포크
bash /path/to/benchbook/integrations/autoresearch/launch_local.sh 4   # benchbook ar init + GPU마다 worktree와 에이전트
benchbook serve                                                  # http://127.0.0.1:7466
```

각 에이전트는 `program.benchbook.md`(업스트림 `program.md`에 공유 루프를 더한 문서)를 따른다. 같은 코드는 같은
하드웨어에서 두 번 돌지 않고, 모든 실행은 크래시까지 기록되며, 다른 에이전트 두 명이 다시 돌려 평균이 여전히
이길 때만 공유 기준선이 된다. 셀프호스팅이고 API 키가 없다. 여러 머신이면 Postgres URL 하나만 있으면 된다.
자세한 내용: [docs/autoresearch.md](docs/autoresearch.md) (영어).

## 벤치마크

<!-- 보고서: bench/results/2026-11-21-claude-sonnet-5-5-rerun/report.md (bench d65441a, claude-sonnet-5-5,
     Claude Code 2.1.330, 구성마다 3회, 실행당 USD 5 상한). 1판은 bench/results/2026-11-20-claude-sonnet-5-5/에 있고,
     달라진 점은 재실행 폴더의 README에 있다. -->

circle packing(n=26), 모델, API 예산, worker 수, 실행 시간 상한을 같게 했다(3회 평균 ± 표준편차).

| | 유실된 결과 | 중복 평가 | 리드 컨텍스트(최대) | 1k token당 best |
|---|---|---|---|---|
| A: subagent가 텍스트로 보고 | 0 | 29.4% ± 15 | 30.5k ± 1.3k | 0.0281 ± 0.0012 |
| B: subagent가 JSON 파일 기록 | 0.33 ± 0.58 | 16.1% ± 5.4 | 35.6k ± 3.1k | 0.0139 ± 0.0011 |
| C: benchbook | 0 | 22.5% ± 10 | 33.1k ± 2.0k | 0.0085 ± 0.0020 |

출시 게이트(MANUAL_QA.md), C 대 A:

- 결과 유실 0: **통과** (C의 모든 실행에서 0. A도 유실이 없었고 B는 한 실행에서 평가 1건을 잃었다).
- 중복 비율이 A보다 낮음: 평균 기준 **통과** (22.5% 대 29.4%. 3회 실행에서는 표준편차 하나 안의 차이다. B의 결과
  파일 방식이 16.1%로 더 낮았다).
- 리드 최대 컨텍스트가 A보다 낮음: **미달** (33.1k 대 30.5k token).

모든 구성이 같은 best(2.636)에 도달했다. 리드와 subagent를 합쳐 C는 A의 4.2배 token, 1.8배 API 비용을 썼다(B는
3.2배, 1.5배). C의 token이 어디에 쓰였는지는
[cost-breakdown.md](bench/results/2026-11-21-claude-sonnet-5-5-rerun/cost-breakdown.md)에 있다. 큰 도구 결과보다는
모델 호출 횟수가 늘고 호출마다 컨텍스트를 다시 읽는 것이 대부분이다.

**Fan-in(H5, 2026-11-25).** 같은 과제를 라운드당 worker 8, 16, 30개로 요청해 A와 C를 비교했다. 실행당 USD 5 상한(완료
실행의 평균: 8과 16은 구성마다 3회, 30은 A 2회, C 1회).

| worker | 리드 컨텍스트(최대), A | 리드 컨텍스트(최대), C | 리드가 받은 보고 token, A | 리드가 받은 보고 token, C |
|---|---|---|---|---|
| 8 | 47.2k | 55.2k | 15.4k | 6.9k |
| 16 | 83.5k | 85.9k | 31.1k | 13.8k |
| 30 | 150.8k | 130.7k (Haiku worker) | 59.1k | 22.3k (Haiku worker) |

사전 등록한 H5 게이트(모든 폭에서 C가 A보다 낮고 더 느리게 늘어남): **미달**. C의 리드가 받는 보고 token은 A의 절반
이하지만, 리드 최대 컨텍스트는 8과 16에서 C가 더 높다. 30에서는 C의 두 실행 모두 같은 조건의 표본이 아니다. USD 5 안에
들어오려고 한 리드는 worker를 Haiku로 돌렸고, 다른 리드는 90개 중 28개만 돌렸다. 폭 30은 Claude Code 2.1.331로 돌았다.
자세한 내용: [bench/results/2026-11-25-h5-sweep/](bench/results/2026-11-25-h5-sweep/README.md).

## 원장의 작동 방식

- **유실 없음.** 검사 실행과 평가는 끝나는 순간 한 행이 된다. 레코드를 인용하지 않은 subagent 출력은 hook이 자동으로
  메모(`auto-captured`)로 남긴다.
- **반복 없음.** 실패한 접근은 실패 signature와 함께 brief에 나온다. 실험에서는 같은 설정 + 코드 + seed가 거부되고,
  seed만 다르면 재현(replicate)으로 집계된다.
- **증거 없는 신뢰 없음.** 에이전트는 주장을 제안하고, 리드나 hook이나 사람이 LLM이 아닌 오라클과 증거 id로 검증한다.
  코드는 깨끗한 재실행, 실험은 baseline을 여전히 이기는 여러 seed의 재현이 증거다.

이 약속이 기대는 것과 기대지 않는 것: DB 트리거는 레코드의 UPDATE, DELETE, TRUNCATE를 막고 에이전트의 role을
바꾸지 못하게 한다. 그래서 "테스트 데이터 정리" 같은 정직한 실수는 기록을 바꾸지 못한다. 하지만 DB owner
자격 증명을 가진 쪽은 막지 못하고(공유 URL을 쓰는 모든 에이전트가 지금은 이 자격 증명을 갖는다), 에이전트 이름은
자기 신고라 이름을 속이는 에이전트는 리드나 "다른 에이전트"로 행세할 수 있다. owner URL을 함께 쓰는 동안에는
실수에 대한 보호로 보고, 악의적인 에이전트에 대한 보호로 보지 않는다. 머신마다 `benchbook_writer` 로그인과
에이전트별 token을 주면 모든 쓰기가 그 에이전트 이름에 묶인다([여러 머신](docs/multi-machine.md#per-agent-tokens)).
owner 연결은 여전히 이를 우회할 수 있다. [Honest limits](docs/concepts.md#honest-limits) 참고.

## 맞는 일

benchbook은 두 종류의 일에서 가치를 낸다.

- **테스트가 있는 코딩 작업.** 에이전트에게 맡긴 조각이 선언된 검사로 판정할 수 있는 commit으로 끝나는 일이다. 변경이
  여러 에이전트와 세션에 걸칠수록 "끝났다" 뒤에 재실행이 있어야 한다.
- **실험**, 아래 세 조건을 모두 채울 때:
  - **시도마다 숫자 지표.** 시도마다 지표가 나온다(`val_bpb`, `sum_radii`).
  - **재실행 가능.** 같은 설정과 코드를 seed만 바꿔 다시 돌릴 수 있어 재실행으로 결과를 검증할 수 있다.
  - **같은 공간의 동시 탐색.** 여러 worker가 같은 탐색 공간을 동시에 탐색해서, 서로의 결과를 읽으면 중복이 줄어든다.

## 무엇이 아닌가

- 코드 리뷰어나 테스트 작성기가 아니다. 선언한 검사를 다시 돌릴 뿐이라서, 검사가 다루지 않는 요구 사항은 재실행이
  통과해도 아무것도 말해 주지 않는다.
- 코드 구현의 중복을 막는 도구가 아니다. 접근은 자유 문장이라서 정확한 해시 기반 중복 차단과 seed 재현은 실험에만
  적용된다. 코드에서 기억하는 것은 어떤 접근이 왜 실패했는지다.
- 한 에이전트나 사용자를 위한 메모리 계층(mem0, Letta)이 아니다.
- 오케스트레이터가 아니다. 에이전트를 실행하거나 스케줄링하지 않고 Claude Code, Codex, Cursor나 직접 만든 루프 밑에서
  동작한다.
- 관측 도구가 아니다. trace는 Langfuse/MLflow/Phoenix에 두고, 레코드는 `trace_ref`로 연결만 한다.

## 문서

[코딩 작업](docs/code.md) · [Claude Code](docs/claude-code.md) · [다른 하네스](docs/harnesses.md) ·
[개념](docs/concepts.md) · [스키마와 SQL 예시](docs/schema.md) · [여러 머신](docs/multi-machine.md) ·
[Python SDK](docs/sdk.md) · [circle packing 데모](examples/circle-packing/README.md) ·
[autoresearch](docs/autoresearch.md) · [릴리스](docs/releasing.md)
(문서 본문은 영어)

라이선스: Apache-2.0.
