# benchbook: 에이전트들의 연구 노트

**찾아낸 것은 사라지지 않고, 반복되지 않고, 증거 없이 믿어지지 않는다.**

여러 에이전트가 실험을 돌리면 결과는 텍스트로 리드 에이전트에게 돌아간다. 일부는 compaction으로 요약되어
사라지고, 일부는 아예 전달되지 않으며, 두 worker가 같은 설정을 각자 돌린다. benchbook은 모든 에이전트가 함께 쓰는
Postgres 원장을 준다. worker는 실험이 끝나는 즉시(실패도) 기록하고, 돌리기 전에 이미 해본 것인지 확인하며,
리드에게는 3문장 요약과 레코드 id만 넘긴다. 리드는 대화록 대신 SQL로 만든 예산 안의 brief(최고 결과, 검증된
주장, 반복하지 말 것)를 읽는다. circle packing 벤치마크에서는 아직 이것으로 리드의 최대 컨텍스트가 줄지 않았다(벤치마크 참고).

## 60초 시작

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook     # v0.1.0이 PyPI에 올라가면 `uv tool install benchbook`. Docker 불필요
uv tool update-shell        # `benchbook`을 못 찾으면 한 번. 새 터미널을 열고 Claude Code를 다시 시작
cd your-repo && benchbook init --goal "검증 손실 최소화" --metric val_loss --direction min
claude plugin marketplace add Jake-Network/benchbook && claude plugin install benchbook@benchbook benchbook-mcp@benchbook
```

Claude Code에서:

```
/benchbook-mcp:benchbook-lead train.py의 val_loss 최소화, 3라운드, worker 4개
```

`benchbook serve` 후 http://127.0.0.1:7466 에서 진행을 본다.

Claude Code 없이도 CLI와 Python SDK로 같은 일을 한다.

```bash
export BENCHBOOK_AGENT=worker-1   # 누가 기록하는지
benchbook exp tried  --config '{"lr": 3e-4, "warmup": 500}'
benchbook exp record --config '{"lr": 3e-4, "warmup": 500}' --metrics '{"val_loss": 2.91}' --seed 0
benchbook brief
```

## autoresearch를 GPU 여러 대에서

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

## 작동 방식

- **유실 없음.** 평가가 끝나는 순간 한 행이 된다. 레코드를 인용하지 않은 subagent 출력은 hook이 자동으로
  메모(`auto-captured`)로 남긴다.
- **반복 없음.** 같은 설정 + 코드 + seed는 거부되고, seed만 다르면 재현(replicate)으로 집계된다. brief는 내 작업
  주변에서 이미 해본 것(실패 포함)을 보여 준다.
- **증거 없는 신뢰 없음.** worker는 주장을 제안한다. 리드나 사람은 LLM이 아닌 오라클과 증거 id로 검증한다.
  기본 rerun 기준은 원래 설정과 코드로 실행한 서로 다른 seed 3개와 다른 에이전트의 재현 1건이고, 재실행 결과가
  baseline을 여전히 이겨야 한다. 다시 돌리기만 하고 재현되지 않은 결과는 거부된다.

이 약속이 기대는 것과 기대지 않는 것: DB 트리거는 레코드의 UPDATE, DELETE, TRUNCATE를 막고 에이전트의 role을
바꾸지 못하게 한다. 그래서 "테스트 데이터 정리" 같은 정직한 실수는 기록을 바꾸지 못한다. 하지만 DB owner
자격 증명을 가진 쪽은 막지 못하고(공유 URL을 쓰는 모든 에이전트가 지금은 이 자격 증명을 갖는다), 에이전트 이름은
자기 신고라 이름을 속이는 에이전트는 리드나 "다른 에이전트"로 행세할 수 있다. owner URL을 함께 쓰는 동안에는
실수에 대한 보호로 보고, 악의적인 에이전트에 대한 보호로 보지 않는다. 머신마다 `benchbook_writer` 로그인과
에이전트별 token을 주면 모든 쓰기가 그 에이전트 이름에 묶인다([여러 머신](docs/multi-machine.md#per-agent-tokens)).
owner 연결은 여전히 이를 우회할 수 있다. [Honest limits](docs/concepts.md#honest-limits) 참고.

## 맞는 일

benchbook은 아래 세 조건을 모두 채우는 일에서 가치를 낸다.

- **시도마다 숫자 지표.** 시도마다 지표가 나온다(`val_bpb`, `sum_radii`).
- **재실행 가능.** 같은 설정과 코드를 seed만 바꿔 다시 돌릴 수 있어 재실행으로 결과를 검증할 수 있다.
- **같은 공간의 동시 탐색.** 여러 worker가 같은 탐색 공간을 동시에 탐색해서, 서로의 결과를 읽으면 중복이 줄어든다.

## 무엇이 아닌가

subagent에게 나눠 맡긴 코드 구현의 중복을 막는 도구가 아니다. 시도마다 숫자 지표가 없고 접근이 자유
문장이라서 중복 차단(설정과 `code_ref`의 정확한 해시)과 seed 기반 재실행 검증이 작동하지 않는다. 맡긴 코딩에
`code` pack이 하는 일은 더 좁다. 보고된 commit에서 선언된 검사를 다시 돌리고 그 완료가 맞았는지 기록한다(다음 절).

메모리 계층(mem0, Letta)이 아니고, 에이전트를 실행하거나 스케줄링하는 오케스트레이터가 아니며, trace를 저장하는
관측 도구도 아니다(`trace_ref`로 연결만 한다).

## 코딩 작업: 완료와 검증된 완료

구현자가 "끝났다"고 한 것과 독립 재실행이 확인한 완료는 다른 기록이다. `code` pack(`benchbook init --pack code`)에서는
완료 조건 검사를 한 번 선언하고, 리드가 통합 단계에 한 줄을 더한다.

```toml
# .benchbook/config.toml
[checks.pytest-media]
cmd = "uv run pytest tests/media --junitxml={junit}"
junit = true
[guard]
paths = ["tests/**", "**/conftest.py", ".benchbook/config.toml"]
```

```bash
benchbook check verify media-upload-dedupe a1b2c3d --reported-by implementer-1
```

이 명령은 그 commit을 깨끗한 worktree에서 선언된 검사로 다시 돌리고, 보고와 재실행을 한 쌍으로 기록하고, commit이
테스트나 검사 정의를 바꿨는지 남긴다(바꿨다면 재실행이 통과해도 사람이 판정한다). pack에는 구현자 자신의 실행을
기록하는 `check run`과 hook, Claude Code 완료 게이트, 조각의 경로 점유(`benchbook piece start`), 범위 밖 발견 캡처,
PR마다 증거 요약 댓글 하나를 다는 GitHub Action도 있다. 자세한 내용은 [docs/code.md](docs/code.md).

"끝났다"가 실제로 끝나지 않은 비율은 아직 재지 않았다. 측정 하네스와 사전 등록 문서는 `bench/code/`에 있고, 현재
상태는 [bench/results/2026-11-21-phase1/report.md](bench/results/2026-11-21-phase1/report.md)에 있다.

## 문서

[개념](docs/concepts.md) · [스키마와 SQL 예시](docs/schema.md) · [Claude Code](docs/claude-code.md) ·
[여러 머신](docs/multi-machine.md) · [Python SDK](docs/sdk.md) · [circle packing 데모](examples/circle-packing/README.md) ·
[autoresearch](docs/autoresearch.md) · [코딩 작업](docs/code.md) · [릴리스](docs/releasing.md)
(문서 본문은 영어)

라이선스: Apache-2.0.
