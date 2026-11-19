# Launch post outline: autoresearch on many GPUs

> Draft outline (§17.4). Numbers come only from the pre-registered launch experiment
> (bench/autoresearch/PREREGISTRATION.md and its report.md). Post nothing about upstream until results exist,
> and then only where a human decides (Discussions or X; no PRs or issues on karpathy/autoresearch).

## English

1. **The pain** (2 paragraphs)
   - autoresearch is one agent, one GPU, one branch. With 8 GPUs you get 8 branches that share nothing:
     the same idea runs on several GPUs, and every `results.tsv` lives on one machine.
   - The keep/discard rule trusts a single 5-minute run. A lucky run becomes the branch's new base and every
     later experiment builds on it. Show the measured share of arm A keeps that did not reproduce (metric 5).
2. **The design** (3 short sections, one diagram)
   - One ledger, self-hosted: embedded Postgres on one machine, a Postgres URL for several; no API key.
   - Refuse before running: the normalised code hash per hardware class and harness; intents for ideas in flight.
   - Build only on replicated results: candidate → two re-runs by other agents → verified; a deterministic
     referee, no LLM judge. Show the T1-T7 table in one image.
3. **The experiment** (pre-registered; link the commit of PREREGISTRATION.md)
   - Conditions: GPUs, hours, model, budgets.
   - Table: metrics 1, 3, 5, 6 for A and B, including the ones B loses. The best-over-time plot.
   - What it cost: the replication share of GPU time; tokens and dollars.
4. **Try it** (3 lines from the README) and the template repository.
5. **What is not done**: public hub, accounts, LLM-judged verification (deliberately absent).

## 한국어

1. **문제** (두 문단)
   - autoresearch는 에이전트 하나, GPU 하나, 브랜치 하나다. GPU가 8장이면 브랜치 8개가 서로 아무것도 공유하지
     않는다. 같은 아이디어가 여러 GPU에서 돌고, `results.tsv`는 머신마다 따로 남는다.
   - keep/discard 판단은 5분짜리 실행 한 번을 믿는다. 운 좋은 값이 브랜치의 새 기준이 되고 이후 실험이 모두 그 위에
     쌓인다. A arm의 keep 중 재현되지 않은 비율(지표 5)을 측정값으로 보여 준다.
2. **설계** (짧은 절 세 개, 그림 하나)
   - 셀프호스팅 원장 하나: 한 머신은 임베디드 Postgres, 여러 머신은 Postgres URL. API 키 없음.
   - 실행 전 차단: 하드웨어 등급과 하네스별 정규화 코드 해시, 진행 중 아이디어는 intent로 공유.
   - 재현된 결과 위에서만 쌓기: 후보 → 다른 에이전트 두 명의 재실행 → verified. 판정은 LLM이 아니라 결정적
     referee가 한다. T1~T7 표를 그림 하나로.
3. **실험** (사전 등록, PREREGISTRATION.md 커밋 링크)
   - 조건: GPU, 시간, 모델, 예산.
   - 표: A와 B의 지표 1, 3, 5, 6. B가 진 지표도 포함. 시간에 따른 최고 기록 그래프.
   - 비용: 재현에 쓴 GPU 시간 비율, 토큰과 달러.
4. **시작하기** (README의 세 줄)와 템플릿 저장소.
5. **하지 않은 것**: 공개 허브, 계정, LLM 판정 검증(의도적으로 없음).
