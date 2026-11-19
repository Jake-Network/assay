# assay

**에이전트들의 연구 노트. 찾아낸 것은 사라지지 않고, 반복되지 않고, 증거 없이 믿어지지 않는다.**

여러 에이전트가 실험을 돌리면 결과는 텍스트로 리드 에이전트에게 돌아간다. 일부는 compaction으로 요약되어
사라지고, 일부는 아예 전달되지 않으며, 두 worker가 같은 설정을 각자 돌린다. assay는 모든 에이전트가 함께 쓰는
Postgres 원장을 준다. worker는 실험이 끝나는 즉시(실패도) 기록하고, 돌리기 전에 이미 해본 것인지 확인하며,
리드에게는 3문장 요약과 레코드 id만 넘긴다. 리드는 대화록 대신 SQL로 만든 예산 안의 brief(최고 결과, 검증된
주장, 반복하지 말 것)를 읽는다.

## 60초 시작

```bash
uv tool install --from git+https://github.com/Jake-Network/assay assay     # PyPI 이름은 미정. Docker 불필요
cd your-repo && assay init --goal "검증 손실 최소화" --metric val_loss --direction min
claude plugin marketplace add Jake-Network/assay && claude plugin install assay@assay
```

Claude Code에서:

```
/assay:assay-lead train.py의 val_loss 최소화, 3라운드, worker 4개
```

`assay serve` 후 http://127.0.0.1:7466 에서 진행을 본다.

Claude Code 없이도 CLI와 Python SDK로 같은 일을 한다.

```bash
assay exp tried  --config '{"lr": 3e-4, "warmup": 500}'
assay exp record --config '{"lr": 3e-4, "warmup": 500}' --metrics '{"val_loss": 2.91}' --seed 0
assay brief
```

## 작동 방식

- **유실 없음.** 평가가 끝나는 순간 한 행이 된다. 레코드를 인용하지 않은 subagent 출력은 hook이 자동으로
  메모(`auto-captured`)로 남긴다.
- **반복 없음.** 같은 설정 + 코드 + seed는 거부되고, seed만 다르면 재현(replicate)으로 집계된다. brief는 내 작업
  주변에서 이미 해본 것(실패 포함)을 보여 준다.
- **증거 없는 신뢰 없음.** worker는 주장을 제안한다. 리드나 사람은 LLM이 아닌 오라클과 증거 id로 검증한다.
  기본 rerun 기준은 원래 설정과 코드로 실행한 서로 다른 seed 3개와 다른 에이전트의 재현 1건이다.
  DB 트리거가 레코드의 수정과 삭제를 막는다.

## 무엇이 아닌가

메모리 계층(mem0, Letta)이 아니고, 에이전트를 실행하거나 스케줄링하는 오케스트레이터가 아니며, trace를 저장하는
관측 도구도 아니다(`trace_ref`로 연결만 한다).

## 문서

[개념](docs/concepts.md) · [스키마와 SQL 예시](docs/schema.md) · [Claude Code](docs/claude-code.md) ·
[여러 머신](docs/multi-machine.md) · [Python SDK](docs/sdk.md) · [circle packing 데모](examples/circle-packing/README.md)
(문서 본문은 영어)

라이선스: Apache-2.0.
