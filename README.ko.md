# benchbook: 테스트가 확인하기 전까지 "완료"는 주장일 뿐이다

**코딩 에이전트가 "끝났다"고 보고하면 benchbook은 바로 그 commit에서 검사를 다시 돌리고, 통과하려고 약하게 만든
테스트를 잡아내며, 검증된 사실을 subagent, 세션, compaction 너머로 이어 준다.**

SWE-bench Verified에서 Claude Code가 완료라고 보고한 39개 중 7개가 재실행에서 벤치마크 테스트를 통과하지 못했고, 39개
에이전트 중 먼저 테스트를 돌린 것은 3개뿐이었다([결과](docs/results.md)). benchbook은 저장소 옆에 Postgres 원장을 둔다.
보고는 주장이 되고, 선언된 검사를 깨끗한 worktree에서 독립 재실행한 결과만이 그것을 `verified`로 바꾼다.

## 시작 (Claude Code)

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook   # Docker 불필요
benchbook install --no-cli       # hook plugin. --mcp를 붙이면 MCP 서버와 worker 에이전트도
cd your-repo && benchbook init   # 저장소의 테스트와 lint 명령을 검사로 제안한다
```

그다음은 하던 대로 일하면 된다. 약속은 하나다. 구현자는 보고 끝에 `DONE <sha>`를 쓰고, 조각 이름은 브랜치 이름,
`piece: <이름>` 줄, `Benchbook-Piece:` commit trailer 중 하나로 정해진다.

## 얻는 것

- **모든 "완료"를 다시 돌린다.** subagent가 `DONE <sha>`를 보고하면 그 commit을 깨끗한 worktree에서 선언된 검사로
  백그라운드에서 돌리고, 다음 턴에 결과를 한 번 전한다:
  `benchbook: media-upload a1b2c3d: re-run passed (2 checks, 14.2s), tests not weakened`.
- **약해진 테스트는 초록불이어도 사람이 본다.** assert 삭제나 변경, 새 `skip`/`xfail`, `conftest.py` 변경,
  merge-base 때보다 적게 돈 테스트.
- **"통과" 기준을 바꿀 수 없다.** 검사는 `.benchbook/config.toml`에 한 번 선언하고 원장에 고정한다. 구현자 자신의
  실행도 기록되지만 재실행으로 치지 않는다.
- **실패는 기억된다.** 실패한 재실행은 실패 signature와 함께 버린 접근으로 남고, 다음 에이전트는 반복하지 말라는 말을
  듣는다.
- **세션 시작과 compaction 뒤의 brief**(실험 단계): 검증된 조각, 판정 안 된 재실행, 버린 접근, 점유된 경로를 최대
  1,500 token에 담는다.

hook은 stderr에 쓰지 않고 항상 0으로 끝나며, benchbook 프로젝트 밖에서는 아무것도 하지 않는다.
[언제 무엇이 일어나나](docs/claude-code.md#what-happens-in-a-session) · [검증 정책](docs/code.md) (영어)

## 리드가 쓰는 명령

```bash
benchbook --agent lead check verify media-upload a1b2c3d --reported-by implementer-1   # 재실행하고 판정
benchbook brief --here            # 이 브랜치가 바꾼 범위의 brief
benchbook status                  # 검증됨, 판정 안 됨, 보고만 됨, 불일치; 열린 발견; hook 상태
benchbook piece start media-upload --paths 'src/media/**'   # 두 구현자가 부딪히지 않게 경로를 점유
```

[GitHub Action](integrations/github-action/README.md)은 pull request가 완료한 조각마다 다시 돌리고, 어떤 완료가
확인됐는지 댓글 하나로 유지한다. Codex, Cursor, Gemini CLI, OpenCode, Pi도 brief와 검사 기록을 받는다
([하네스](docs/harnesses.md)). 자동 재실행은 Claude Code에서만 된다.

## 지금까지 잰 것

- **"완료"는 자주 완료가 아니다.** `claude-sonnet-5-5`로 SWE-bench Verified: 보고된 완료 39개 중 7개가 재실행에서
  벤치마크 검사를 통과하지 못했다(17.9%, 95% CI 9.0%~32.7%). benchbook이 실제로 하듯 보이는 테스트만 다시 돌리면 그중
  3개를 잡았다(그중 둘은 정답 패치도 같은 테스트에서 실패한다).
- **재시작 너머의 기억은 아직 증명되지 않았다.** 한 번씩 돌린 파일럿 네 번에서 brief를 읽은 리드는 진행 파일을 쓴
  리드나 세션을 이어 간 리드보다 상태 질문에 덜 답했다. compaction까지 간 파일럿은 없다.
- **실험 swarm은 잃는 것이 없다.** circle packing: 잃은 결과 0, 중복 평가는 일반 subagent보다 적었다(22.5% 대 29.4%,
  표준편차 안). 대신 API 비용은 1.8배이고 리드 context 최대치가 더 높았다.

방법, 모든 파일럿, 원 보고서: [docs/results.md](docs/results.md) (영어).

## 실험 swarm

benchbook은 실험하는 에이전트의 연구 노트로 시작했다. worker는 실패를 포함해 모든 실행을 기록하고 돌리기 전에 이미
해 본 것인지 확인한다. 같은 config, 코드, seed는 거절되고 seed만 다르면 replicate로 센다. 리드는 transcript 대신
예산이 정해진 brief를 읽는다.

```bash
cd your-repo && benchbook init --goal "Minimise validation loss" --metric val_loss --direction min
benchbook install --no-cli --mcp
```

그다음 Claude Code에서 `/benchbook-mcp:benchbook-lead Minimise val_loss of train.py; 3 rounds, 4 workers`, 지켜보려면
`benchbook serve`(http://127.0.0.1:7466). GPU 여러 대에서는 다른 에이전트 둘이 재현해야 결과가 공유 기준이 된다:
[autoresearch](docs/autoresearch.md).

## 맞는 일

- **테스트 스위트가 있는 코딩 작업**: 여러 에이전트나 세션에 나눠 맡기고, 조각마다 검사로 판정할 수 있는 commit에서
  끝나는 일.
- **실험**, 세 가지를 모두 갖출 때: **시도마다 숫자 지표**(모든 시도가 metric을 낸다), **재실행 가능**(seed만 바꿔
  같은 config를 다시 돌릴 수 있다), **같은 공간의 동시 탐색**(여러 worker가 같은 공간을 함께 찾는다).

## 무엇이 아닌가

- 코드 리뷰어나 테스트 작성기가 아니다. 검사가 다루지 않는 것은 초록불 재실행도 말해 주지 않는다.
- 코드 구현의 중복 차단기가 아니다. 코드에서는 정확한 반복이 아니라 어떤 접근이 왜 실패했는지를 기억한다.
- orchestrator도, 단일 에이전트 메모리(mem0, Letta)도, observability 도구도 아니다(레코드에 `trace_ref`를 남길 수 있다).
- owner URL을 공유할 때 악의적인 에이전트를 막는 장치가 아니다. 보장은 실수를 잡는다([정직한 한계](docs/concepts.md#honest-limits)).

## 문서

[코딩 작업](docs/code.md) · [Claude Code](docs/claude-code.md) · [다른 하네스](docs/harnesses.md) ·
[결과](docs/results.md) · [개념](docs/concepts.md) · [스키마와 SQL](docs/schema.md) ·
[여러 머신](docs/multi-machine.md) · [Python SDK](docs/sdk.md) · [autoresearch](docs/autoresearch.md) ·
[circle packing 데모](examples/circle-packing/README.md) · [릴리스](docs/releasing.md) (모두 영어)

License: Apache-2.0.
