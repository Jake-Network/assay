# benchbook: 에이전트 swarm을 위한 Postgres 원장

**subagent의 결과는 리드의 context가 아니라 Postgres에 쌓인다. 리드는 SQL로 꺼내 읽으므로 swarm이 커져도 리드의
context window가 차지 않는다.**

수십 개의 subagent에게 일을 나눠 주는 리드 에이전트는 모든 결과를 자연어 텍스트로 돌려받는다. worker가 많아지면 그
보고가 context window를 채우고, 다음 compaction이나 세션에서 그 내용은 사라진다. benchbook은 저장소 옆에 원장을 둔다.
worker는 실험, 검사 실행, 완료 보고를 끝나는 즉시 기록하고 record id만 돌려주며, 리드는 필요한 것을 원장에서 꺼내 쓴다.

## 시작

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook   # Docker 불필요
benchbook install --no-cli --mcp   # Claude Code plugin: hook, MCP 서버, worker 에이전트
```

**코딩 swarm.** `cd your-repo && benchbook init`은 저장소의 테스트와 lint 명령을 검사로 제안한다. 그다음은 하던 대로
일하면 되고, 약속은 하나다. 구현자는 보고 끝에 `DONE <sha>`를 쓴다.

**실험 swarm.** `benchbook init --goal "Minimise validation loss" --metric val_loss --direction min` 다음 Claude
Code에서 `/benchbook-mcp:benchbook-lead Minimise val_loss of train.py; 3 rounds, 4 workers`, 지켜보려면
`benchbook serve`(http://127.0.0.1:7466).

## 얻는 것

- **결과는 원장에, context에는 id만.** worker는 실패를 포함해 모든 실행을 기록하고 record id가 담긴 짧은 요약만
  돌려준다. 리드는 예산이 정해진 brief(최고 결과, 남은 일, 반복하지 말 것)를 읽고 필요할 때 SQL로 자세히 묻는다.
- **잃는 것이 없다.** 검사 실행과 평가는 끝나는 순간 한 행이 된다. 규칙을 무시한 subagent의 보고도 hook이 잡아 둔다.
- **반복하지 않는다.** 같은 config, 코드, seed는 거절되고 seed만 다르면 replicate로 센다. 실패한 접근은 실패
  signature와 함께 brief에 남는다.
- **세션과 compaction 너머**(실험 단계): 새 세션이나 compaction 뒤의 세션은 누군가의 기억이 아니라 brief에서 시작한다.
- **결과는 쌓아 올리기 전에 확인한다.** 실험 결과는 다른 에이전트가 재현한 뒤에야 기준이 되고, worker의 `DONE <sha>`는
  그 commit의 깨끗한 worktree에서 다시 돌린다([코딩 작업](docs/code.md)).

hook은 stderr에 쓰지 않고 항상 0으로 끝나며, benchbook 프로젝트 밖에서는 아무것도 하지 않는다.

## 리드가 쓰는 명령

```bash
benchbook brief --here            # 이 브랜치에서 swarm이 한 일, token 예산 안에서
benchbook status                  # 검증됨, 판정 안 됨, 보고만 됨, 불일치; 열린 발견; hook 상태
benchbook sql "SELECT ..."        # 그 밖의 모든 것, 읽기 전용
benchbook get c:7f3a91c2          # 레코드 하나를 최대 400 token으로
```

[GitHub Action](integrations/github-action/README.md)은 pull request가 완료한 조각마다 다시 돌린다. Codex, Cursor,
Gemini CLI, OpenCode, Pi도 brief와 검사 기록을 받는다([하네스](docs/harnesses.md)). Python SDK와 CLI는 하네스 없이도
쓸 수 있다.

## 지금까지 잰 것

- **리드가 읽는 보고 token은 절반 이하지만, context 최대치는 아직 낮지 않다.** worker 8, 16, 30명의 circle packing:
  리드에게 간 보고 token은 benchbook이 6.9k, 13.8k, 22.3k, 일반 subagent가 15.4k, 31.1k, 59.1k였다. 리드 context
  최대치는 8명에서 55.2k 대 47.2k, 16명에서 85.9k 대 83.5k였다(30명은 조건이 달라 비교할 수 없다).
- **swarm은 잃는 것이 없다.** 잃은 결과 0, 중복 평가는 일반 subagent보다 적었다(22.5% 대 29.4%, 표준편차 안). 대신
  API 비용은 1.8배였다.
- **재시작 너머의 기억은 증명되지 않았다.** 한 번씩 돌린 파일럿 네 번에서 brief를 읽은 리드는 진행 파일을 쓴 리드보다
  상태 질문에 덜 답했다. compaction까지 간 파일럿은 없다.
- **worker의 "완료"는 확인이 필요하다.** SWE-bench Verified에서 Claude Code가 완료라고 보고한 39개 중 7개가 재실행에서
  벤치마크 테스트를 통과하지 못했다(17.9%, 95% CI 9.0%~32.7%).

방법, 모든 파일럿, 원 보고서: [docs/results.md](docs/results.md) (영어).

## 맞는 일

- **테스트 스위트가 있는 코딩 작업**: 여러 에이전트나 세션에 나눠 맡기고, 조각마다 검사로 판정할 수 있는 commit에서
  끝나는 일.
- **실험**, 세 가지를 모두 갖출 때: **시도마다 숫자 지표**(모든 시도가 metric을 낸다), **재실행 가능**(seed만 바꿔
  같은 config를 다시 돌릴 수 있다), **같은 공간의 동시 탐색**(여러 worker가 같은 공간을 함께 찾는다). GPU 여러 대에서는
  [autoresearch](docs/autoresearch.md).

## 무엇이 아닌가

- orchestrator가 아니다. 에이전트를 돌리거나 배정하지 않고 Claude Code, Codex, Cursor나 직접 만든 loop 아래에 놓인다.
- 코드 리뷰어나 테스트 작성기가 아니다. 재실행은 선언된 검사가 다루는 것만 확인한다.
- 코드 구현의 중복 차단기가 아니다. 코드에서는 정확한 반복이 아니라 어떤 접근이 왜 실패했는지를 기억한다.
- 단일 에이전트 메모리(mem0, Letta)도, observability 도구도 아니다(레코드에 `trace_ref`를 남길 수 있다).
- owner URL을 공유할 때 악의적인 에이전트를 막는 장치가 아니다. 보장은 실수를 잡는다([정직한 한계](docs/concepts.md#honest-limits)).

## 문서

[개념](docs/concepts.md) · [코딩 작업](docs/code.md) · [Claude Code](docs/claude-code.md) · [다른 하네스](docs/harnesses.md) ·
[결과](docs/results.md) · [스키마와 SQL](docs/schema.md) · [여러 머신](docs/multi-machine.md) ·
[Python SDK](docs/sdk.md) · [autoresearch](docs/autoresearch.md) · [circle packing 데모](examples/circle-packing/README.md) ·
[릴리스](docs/releasing.md) (모두 영어)

License: Apache-2.0.